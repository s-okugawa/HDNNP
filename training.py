# -*- coding: utf-8 -*-

# define variables
from config import file_
from config import mpi

# import python modules
from os import path
from shutil import copy2
from datetime import datetime
from time import time
from mpi4py import MPI

# import own modules
from modules.data import DataGenerator
from modules.model import HDNNP
from modules.util import mpimkdir
from modules.util import mpisave

start = time()
datestr = datetime.now().strftime('%m%d-%H%M%S')
save_dir = path.join(file_.save_dir, datestr)
out_dir = path.join(file_.out_dir, datestr)
mpimkdir(save_dir)
mpimkdir(out_dir)
progress = MPI.File.Open(mpi.comm, path.join(out_dir, 'progress.dat'), MPI.MODE_CREATE | MPI.MODE_WRONLY)
copy2('config.py', path.join(out_dir, 'config.py'))

generator = DataGenerator()
for config, dataset in generator:
    output_file = path.join(out_dir, '{}.npz'.format(config))
    hdnnp = HDNNP(dataset.natom, dataset.ninput, dataset.composition)
    hdnnp.load(save_dir)
    hdnnp.fit(dataset, progress=progress)

    hdnnp.save(save_dir, output_file)
    mpi.comm.Barrier()
mpisave(generator, save_dir)
progress.Write('\n\nTotal time: {}'.format(time()-start))
progress.Close()