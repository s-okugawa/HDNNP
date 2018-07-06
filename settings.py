# -*- coding: utf-8 -*-

###########################################################
#
# this is settings file.
# copy this file to 'settings.py', and edit it.
#
###########################################################

from mpi4py import MPI
import chainermn
from modules.util import DictAsAttributes
from modules.argparser import get_parser

import matplotlib as mpl
mpl.use('Agg')


args = get_parser()


file = DictAsAttributes(
    xyz_file='GaN/debug_benchmark/GaN.xyz',
    config=['all'],
    out_dir='output',
    test_dir='test',
)


mpi = DictAsAttributes(
    comm=MPI.COMM_WORLD,
    rank=MPI.COMM_WORLD.Get_rank(),
    size=MPI.COMM_WORLD.Get_size(),
    gpu=-1,
    chainer_comm=chainermn.create_communicator('naive', MPI.COMM_WORLD),
)


visual = DictAsAttributes(
    fontsize=12
)


sym_func = DictAsAttributes(
    Rc=[5.0],
    eta=[0.01, 0.1, 1.0],
    Rs=[2.0, 3.2, 3.8],
    lambda_=[-1, 1],
    zeta=[1, 2, 4],
)


model = DictAsAttributes(
    epoch=10,
    batch_size=10,
    preproc='pca',
    init_lr=1.0e-3,
    final_lr=1.0e-5,
    lr_decay=0.0e-3,
    mixing_beta=1.0,
    l1_norm=0.0e-4,
    l2_norm=0.0e-4,
    layer=[
        {'node': 30, 'activation': 'tanh'},
        {'node': 30, 'activation': 'tanh'},
        {'node': 1, 'activation': 'identity'},
    ],
    metrics='validation/main/tot_RMSE',
)


gpyopt_bounds = [
    {'name': 'init_lr', 'type': 'continuous', 'domain': (1.0e-4, 1.0e-2)},
    {'name': 'l1_norm', 'type': 'continuous', 'domain': (1.0e-4, 1.0e-2)},
    {'name': 'l2_norm', 'type': 'continuous', 'domain': (1.0e-4, 1.0e-2)},
]
