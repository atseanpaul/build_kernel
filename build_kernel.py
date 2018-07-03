#! /usr/bin/env python3
# Copyright (c) 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import argparse
import configparser
import os
import pathlib
import re
import subprocess
import tempfile
import time

class Builder(object):
  def __init__(self, ini_path, generate_compile_db, generage_pkg,
               fail_on_stderr):
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
                      'its_file': None,
                      'completion_text': None
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
    self.completion_text = cp.get('build', 'completion_text')

    self.generate_pkg = generage_pkg
    self.generate_compile_db = generate_compile_db
    self.fail_on_stderr = fail_on_stderr

    if self.defconfig and self.config_file:
      raise ValueError('Specifying both defconfig and config_file is invalid')

    if self.defconfig:
      postfix = self.defconfig
    else:
      postfix = pathlib.PurePath(self.config_file).name
    if generate_compile_db:
      prefix = 'bear'
    else:
      prefix = 'build'

    self.output_path = pathlib.Path.cwd().joinpath(
                          '.{}_{}-{}'.format(prefix, self.kernel_arch, postfix))
    if not self.output_path.is_dir():
      self.output_path.mkdir()

    self.packed_kernel = self.output_path.joinpath('vmlinux.kpart')


  def __run_command(self, args, fail_on_stderr = False):
    print('')
    print('#############################################################')
    print('#')
    print('# {}'.format(args))
    print('#')
    drm_re = re.compile('drivers/gpu/drm')
    drm_stderr = []
    p = subprocess.Popen(args=args,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    for l in iter(p.stdout.readline, b''):
      print(l.rstrip().decode('utf-8'))

    stderr = False
    for l in iter(p.stderr.readline, b''):
      line = l.rstrip().decode('utf-8')
      print(line)
      if drm_re.search(line):
          drm_stderr.append(line)
      stderr = True;

    p.wait()

    print('***********************************************************')
    print('*')
    if len(drm_stderr):
      print('*              DRM WARNINGS/ERRORS')
      for l in drm_stderr:
        print(l)
      print('***********************************************************')
      raise subprocess.CalledProcessError(p.returncode, args)
    else:
      print('*              DRM BUILD IS CLEAN')
    print('*')
    print('***********************************************************')

    if p.returncode != 0 or (fail_on_stderr and stderr):
      raise subprocess.CalledProcessError(p.returncode, args)


  def __run_make(self, flags=[], env={}, targets=[], root=False, bear=False):
    new_env = {}
    new_env['ARCH'] = self.kernel_arch
    new_env['CROSS_COMPILE'] = self.cross_compile
    new_env['O'] = str(self.output_path)
    #new_env['EXTRA_CFLAGS'] = '-Werror'
    new_env.update(env)

    args = []
    if root:
      args = ['sudo']
    if bear:
      args = args + ['bear']

    args = args + ['make']

    # kernel Makefile is inconsistent with which arguments can be set as env
    # variables, and which are cmdline assignments. So make all env cmdline
    # assignments
    for k,v in new_env.items():
      args.append('{}={}'.format(k, v))

    args.append('-j{}'.format(self.jobs))
    args = args + flags +  targets
    self.__run_command(args, fail_on_stderr=self.fail_on_stderr)


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
          d.write(s.read())
      self.__run_make(targets=['olddefconfig'])


  def __make(self):
    if self.generate_pkg and not self.generate_compile_db:
      self.__run_make(targets=['bindeb-pkg'])
    else:
      self.__run_make(targets=['all'], bear=self.generate_compile_db)

    if self.install_dtbs:
      self.__run_make(targets=['dtbs'])
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

    self.__run_command([
      self.vbutil_kernel,
      '--pack', str(self.packed_kernel),
      '--version', '1',
      '--vmlinuz', str(uimg),
      '--arch', self.vbutil_arch,
      '--keyblock', self.keyblock,
      '--signprivate', self.data_key,
      '--config', str(cmdline),
      '--bootloader', str(zero)])


  def __flash(self):
    if not self.kernel_part_uuid:
      return

    path = '/dev/disk/by-partuuid/{}'.format(self.kernel_part_uuid)
    kernel_part = pathlib.Path(path)
    if not kernel_part.is_block_device():
      print('Insert your USB key...')
      while not kernel_part.is_block_device():
        time.sleep(2)

    # Flash kernel to USB drive
    self.__run_command([
      'sudo',
      'dd',
      'if={}'.format(str(self.packed_kernel)),
      'of={}'.format(str(kernel_part))])
    self.__run_command(['sync'])

    if not self.root_uuid:
      return

    # Copy modules to rootfs
    root = pathlib.Path('/dev/disk/by-uuid/{}'.format(self.root_uuid))
    if not root.is_block_device():
      print('Insert your USB key...')
      while not kernel_part.is_block_device():
        time.sleep(2)

    with tempfile.TemporaryDirectory() as mount_pt:
      self.__run_command([
        'sudo',
        'mount',
        'UUID={}'.format(self.root_uuid),
        mount_pt])
      try:
        if self.install_modules:
          self.__run_make(env={ 'INSTALL_MOD_PATH': mount_pt },
                          targets=['modules_install'], root=True)
        if self.install_dtbs:
          self.__run_make(env={ 'INSTALL_DTBS_PATH': mount_pt },
                          targets=['dtbs_install'], root=True)
      finally:
        self.__run_command([
          'sudo',
          'umount',
          mount_pt])


  def do_build(self):
    if self.generate_compile_db:
        self.__run_make(targets=['mrproper'])

    self.__configure()
    self.__make()

    if not self.generate_compile_db:
        self.__package()
        self.__flash()

    if self.completion_text:
        print(self.completion_text)


def main():
  parser = argparse.ArgumentParser(description='Build a kernel')
  parser.add_argument('--config', help='Optional build config path override',
                      action='append')
  parser.add_argument('--gen_compile_db', default=False, action='store_true',
                      help='Use bear to generate a compilation database')
  parser.add_argument('--gen_pkg', default=False, action='store_true',
                      help='Generate deb packages')
  parser.add_argument('--nofail_on_stderr', default=True, action='store_false',
                      help='Fail command on stderr')
  args = parser.parse_args()

  for c in args.config:
    builder = Builder(c, args.gen_compile_db, args.gen_pkg,
                      args.nofail_on_stderr)
    builder.do_build()


if __name__ == '__main__':
  main()
