#! /usr/bin/env python3
# Copyright (c) 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import argparse
import configparser
import os
import pathlib
import subprocess

class Builder(object):
  def __init__(self, ini_path):
    cp = configparser.SafeConfigParser(
            defaults={'kernel_part_uuid': None,
                      'root_uuid': None,
                      'defconfig': None,
                      'config_file': None,
                      'jobs': 1,
                      'vbutil_kernel': None,
                      'keyblock': None,
                      'data_key': None,
                      'cmdline': None,
                      'vbutil_arch': None,
                      'mkimage': None,
                      'its_file': None
            })
    cp.read(ini_path)

    self.kernel_part_uuid = cp.get('target', 'kernel_part_uuid', raw=True)
    self.root_uuid = cp.get('target', 'root_uuid', raw=True)

    self.defconfig = cp.get('build', 'defconfig', raw=True)
    self.config_file = cp.get('build', 'config_file', raw=True)
    self.kernel_arch = cp.get('build', 'kernel_arch', raw=True)
    self.cross_compile = cp.get('build', 'cross_compile', raw=True)
    self.jobs = cp.getint('build', 'jobs', raw=True)

    self.vbutil_kernel = cp.get('build', 'vbutil_kernel', raw=True)
    self.keyblock = cp.get('build', 'keyblock', raw=True)
    self.data_key = cp.get('build', 'data_key', raw=True)
    self.cmdline = cp.get('build', 'cmdline', raw=True)
    self.vbutil_arch = cp.get('build', 'vbutil_arch', raw=True)

    self.mkimage = cp.get('build', 'mkimage', raw=True)
    self.its_file = cp.get('build', 'its_file', raw=True)

    self.install_modules = cp.getboolean('build', 'install_modules')
    self.install_dtbs = cp.getboolean('build', 'install_dtbs')
    self.generate_htmldocs = cp.getboolean('build', 'generate_htmldocs')

    self.output_path = pathlib.Path.cwd().joinpath(
                                '.build_{}'.format(self.kernel_arch))
    if not self.output_path.is_dir():
      self.output_path.mkdir()

  
  def __run_command(self, args):
    print('')
    print('#############################################################')
    print('#')
    print('# {}'.format(args))
    print('#')
    p = subprocess.Popen(args=args,stdout=subprocess.PIPE)
    for l in iter(p.stdout.readline, b''):
      print(l.rstrip().decode('utf-8'))
    p.wait()
    if p.returncode != 0:
      raise subprocess.CalledProcessError(p.returncode, args)


  def __run_make(self, flags=[], env={}, targets=[]):
    new_env = {}
    new_env['ARCH'] = self.kernel_arch
    new_env['CROSS_COMPILE'] = self.cross_compile
    new_env['O'] = str(self.output_path)
    new_env.update(env)

    args = ['make']
    # kernel Makefile is inconsistent with which arguments can be set as env
    # variables, and which are cmdline assignments. So make all env cmdline
    # assignments
    for k,v in new_env.items():
      args.append('{}={}'.format(k, v))

    args.append('-j{}'.format(self.jobs))
    args = args + flags +  targets
    self.__run_command(args)


  def __configure(self):
    # prefer defconfig over out-of-tree config
    if self.defconfig:
      self.__run_make(targets=[self.defconfig])
    else:
      print('Using out-of-tree config {}'.format(self.config_file))
      config_src_path = pathlib.PosixPath(self.config_file)
      config_dst_path = self.output_path.joinpath('.config')
      with config_src_path.open() as s:
        with config_dst_path.open(mode='w') as d:
          d.write(r.read())


  def __make(self):
    self.__run_make(targets=['Image', 'modules', 'dtbs'])
    if self.install_modules:
      self.__run_make(env={ 'INSTALL_MOD_PATH': str(self.output_path) },
                      targets=['modules_install'])
    if self.install_dtbs:
      self.__run_make(env={ 'INSTALL_DTBS_PATH': str(self.output_path) },
                      targets=['dtbs_install'])
    if self.generate_htmldocs:
      self.__run_make(targets=['htmldocs'])


  def __package(self):
    if not self.mkimage:
      return

    uimg = self.output_path.joinpath('vmlinux.uimg')
    self.__run_command([
      self.mkimage,
       '-D', '""-I dts -O dtb -p 2048""',
       '-f', self.its_file,
       str(uimg)
    ])

    if not self.vbutil_kernel:
      return

    zero = self.output_path.joinpath('zero.bin')
    self.__run_command([
      'dd',
      'if=/dev/zero',
      'of={}'.format(str(zero)),
      'bs=512',
      'count=1'
    ])

    cmdline = self.output_path.joinpath('cmdline')
    with cmdline.open('w') as f:
      f.write(self.cmdline)

    packed = self.output_path.joinpath('vmlinux.kpart')
    self.__run_command([
      self.vbutil_kernel,
      '--pack', str(packed),
      '--version', '1',
      '--vmlinuz', str(uimg),
      '--arch', self.vbutil_arch,
      '--keyblock', self.keyblock,
      '--signprivate', self.data_key,
      '--config', str(cmdline),
      '--bootloader', str(zero)])


  def do_build(self):
    self.__configure()
    self.__make()
    self.__package()


def main():
  parser = argparse.ArgumentParser(description='Build a kernel')
  parser.add_argument('--config', default='build.ini',
                      help='Optional build config path override')
  args = parser.parse_args()

  builder = Builder(args.config)
  builder.do_build()
  

if __name__ == '__main__':
  main()
