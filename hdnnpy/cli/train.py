# coding=utf-8

import os
import pathlib
import pickle
import shutil
import sys

import ase.io
import chainer
import chainer.training.extensions as ext
from chainer.training.triggers import EarlyStoppingTrigger
import chainermn
from traitlets import (Bool, Dict, List, Unicode)
from traitlets.config import Application

from hdnnpy.cli.configurables import (
    DatasetConfig, ModelConfig, Path, TrainingConfig,
    )
from hdnnpy.dataset import (AtomicStructure, DatasetGenerator, HDNNPDataset)
from hdnnpy.format import parse_xyz
from hdnnpy.chainer import (
    Evaluator, HighDimensionalNNP, Manager, MasterNNP, Updater,
    scatter_plot, set_log_scale,
    )
from hdnnpy.utils import (MPI, mkdir, pprint)


class TrainingApplication(Application):
    name = Unicode(u'HDNNP training application')

    is_resume = Bool(False, help='')
    resume_dir = Path(None, allow_none=True, help='')
    verbose = Bool(False, help='').tag(config=True)
    classes = List([DatasetConfig, ModelConfig, TrainingConfig])

    config_file = Path('config.py', help='Load this config file')

    aliases = Dict({
        'resume': 'TrainingApplication.resume_dir',
        'log_level': 'Application.log_level',
        })

    flags = Dict({
        'verbose': ({
            'TrainingApplication': {
                'verbose': True,
                },
            }, 'set verbose mode'),
        'debug': ({
            'Application': {
                'log_level': 10,
                },
            }, 'Set log level to DEBUG'),
        })

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.dataset_config = None
        self.model_config = None
        self.training_config = None

    def initialize(self, argv=None):
        if MPI.rank != 0:
            sys.stdout = pathlib.Path(os.devnull).open('w')
        # temporarily set `resume_dir` configurable
        self.__class__.resume_dir.tag(config=True)
        self.parse_command_line(argv)
        self.__class__.resume_dir.tag(config=False)

        if self.resume_dir is not None:
            self.is_resume = True
            self.config_file = self.resume_dir.with_name(self.config_file.name)
        self.load_config_file(self.config_file)

        self.dataset_config = DatasetConfig(config=self.config)
        self.model_config = ModelConfig(config=self.config)
        self.training_config = TrainingConfig(config=self.config)
        if self.is_resume:
            self.training_config.out_dir = self.resume_dir.parent

        # assertion
        assert self.dataset_config.order == self.model_config.order

    def start(self):
        tc = self.training_config
        mkdir(tc.out_dir)
        try:
            tag_xyz_map, tc.elements = parse_xyz(tc.data_file)
            datasets = self.construct_datasets(tag_xyz_map)
            dataset = DatasetGenerator(*datasets).holdout(tc.train_test_ratio)
            result = self.train(dataset)
            self.dump(result)
        finally:
            if not self.is_resume:
                shutil.copy(self.config_file,
                            tc.out_dir / self.config_file.name)

    def construct_datasets(self, tag_xyz_map):
        dc = self.dataset_config
        tc = self.training_config
        if 'all' in tc.tags:
            included_tags = sorted(tag_xyz_map)
        else:
            included_tags = tc.tags

        preprocess_dir_path = tc.out_dir / 'preprocess'
        mkdir(preprocess_dir_path)
        for preprocess in dc.preprocesses:
            if self.is_resume:
                preprocess.load(preprocess_dir_path
                                / f'{preprocess.__class__.__name__}.npz')

        datasets = []
        for tag in included_tags:
            try:
                xyz_path = tag_xyz_map[tag]
            except KeyError:
                pprint(f'Sub dataset tagged as "{tag}" does not exist.')
                continue

            pprint(f'Construct sub dataset tagged as "{tag}"')
            dataset = HDNNPDataset(descriptor=dc.descriptor,
                                   property_=dc.property_,
                                   order=dc.order)
            structures = [AtomicStructure(atoms) for atoms
                          in ase.io.iread(str(xyz_path),
                                          index=':', format='xyz')]

            descriptor_npz_path = xyz_path.with_name(
                f'{dataset.descriptor_dataset.__class__.__name__}.npz')
            if descriptor_npz_path.exists():
                dataset.descriptor_dataset.load(
                    descriptor_npz_path, verbose=self.verbose)
            else:
                dataset.descriptor_dataset.make(
                    structures, **dc.parameters, verbose=self.verbose)
                dataset.descriptor_dataset.save(
                    descriptor_npz_path, verbose=self.verbose)

            property_npz_path = xyz_path.with_name(
                f'{dataset.property_dataset.__class__.__name__}.npz')
            if property_npz_path.exists():
                dataset.property_dataset.load(
                    property_npz_path, verbose=self.verbose)
            else:
                dataset.property_dataset.make(
                    structures, verbose=self.verbose)
                dataset.property_dataset.save(
                    property_npz_path, verbose=self.verbose)

            dataset.construct(tc.elements, dc.preprocesses, shuffle=True)
            for preprocess in dc.preprocesses:
                preprocess.save(preprocess_dir_path
                                / f'{preprocess.__class__.__name__}.npz')

            dataset.scatter()
            datasets.append(dataset)
            dc.n_sample += dataset.total_size
        return datasets

    def train(self, dataset, comm=None):
        mc = self.model_config
        tc = self.training_config
        if comm is None:
            comm = chainermn.create_communicator('naive', MPI.comm)
        result = {'training_time': 0.0, 'observation': []}

        # model and optimizer
        master = MasterNNP(tc.elements, mc.layers)
        master_opt = chainer.optimizers.Adam(tc.init_lr)
        master_opt = chainermn.create_multi_node_optimizer(master_opt, comm)
        master_opt.setup(master)
        master_opt.add_hook(chainer.optimizer_hooks.Lasso(tc.l1_norm))
        master_opt.add_hook(chainer.optimizer_hooks.WeightDecay(tc.l2_norm))

        for training, test in dataset:
            tag = training.tag

            # iterators
            train_iter = chainer.iterators.SerialIterator(
                training, tc.batch_size // MPI.size, repeat=True, shuffle=True)
            test_iter = chainer.iterators.SerialIterator(
                test, tc.batch_size // MPI.size, repeat=False, shuffle=False)

            # model
            hdnnp = HighDimensionalNNP(
                training.elemental_composition, mc.layers, mc.order,
                **mc.loss_function_params)
            hdnnp.sync_param_with(master)
            main_opt = chainer.Optimizer()
            main_opt = chainermn.create_multi_node_optimizer(main_opt, comm)
            main_opt.setup(hdnnp)

            # triggers
            interval = (tc.interval, 'epoch')
            stop_trigger = EarlyStoppingTrigger(
                check_trigger=interval, monitor=tc.metrics,
                patients=tc.patients, mode='min',
                verbose=self.verbose, max_trigger=(tc.epoch, 'epoch'))

            # updater and trainer
            updater = Updater(
                train_iter, optimizer={'main': main_opt, 'master': master_opt})
            out_dir = tc.out_dir / tag
            trainer = chainer.training.Trainer(updater, stop_trigger, out_dir)

            # extensions
            trainer.extend(ext.ExponentialShift(
                'alpha', 1 - tc.lr_decay,
                target=tc.final_lr, optimizer=master_opt))
            trainer.extend(chainermn.create_multi_node_evaluator(
                Evaluator(test_iter, hdnnp), comm))
            # todo: enable to gather multi node prediction
            trainer.extend(scatter_plot(hdnnp, test, mc.order),
                           trigger=interval)
            if MPI.rank == 0:
                trainer.extend(ext.LogReport(log_name='training.log'))
                trainer.extend(ext.PrintReport(
                    ['epoch', 'iteration', 'main/0th_RMSE', 'main/1st_RMSE',
                     'main/total_RMSE', 'validation/main/0th_RMSE',
                     'validation/main/1st_RMSE', 'validation/main/total_RMSE'],
                    ))
                trainer.extend(ext.PlotReport(
                    ['main/total_RMSE', 'validation/main/total_RMSE'],
                    x_key='epoch', postprocess=set_log_scale,
                    file_name='RMSE.png', marker=None))

            # load trainer snapshot and resume training
            if self.is_resume and tag != self.resume_dir.name:
                pprint(f'Resume training loop from dataset tagged "{tag}"')
                trainer_snapshot = self.resume_dir/'trainer_snapshot.npz'
                interim_result = self.resume_dir/'interim_result.pickle'
                chainer.serializers.load_npz(trainer_snapshot, trainer)
                result = pickle.loads(interim_result.read_bytes())
                # remove snapshot
                MPI.comm.Barrier()
                if MPI.rank == 0:
                    trainer_snapshot.unlink()
                    interim_result.unlink()

            with Manager(tag, trainer, result, is_snapshot=True):
                trainer.run()

        chainer.serializers.save_npz(
            tc.out_dir / f'{master.__class__.__name__}.npz', master)

        return result

    def dump(self, result):
        # todo: implement
        if MPI.rank == 0:
            pass


main = TrainingApplication.launch_instance
