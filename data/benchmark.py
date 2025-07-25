import os
from data import multiscalesrdata as srdata


class Benchmark(srdata.SRData):
    def __init__(self, args, name='', train=True):
        super(Benchmark, self).__init__(
            args, name=name, train=train, benchmark=True
        )

    def _set_filesystem(self, dir_data):
        self.apath = os.path.join(dir_data,'benchmark', self.name)
        self.dir_hr = os.path.join(self.apath, 'HR')
        self.dir_lr = os.path.join(self.apath, 'LR_bicubic')
        self.ext = ('.png','.png')
        print(self.dir_hr)
        print(self.dir_lr)
