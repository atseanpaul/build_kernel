#! /usr/bin/env python3
# Copyright (c) 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import argparse
import configparser
import os
import pathlib
import re
import stat
import subprocess
import tempfile
import threading
import time
import urllib.request

class Builder(object):

  stderr_ignore = [
    re.compile('#warning syscall (io_pgetevents|rseq) not implemented'),
  ]

  def __init__(self, ini_path, generate_compile_db, generate_pkg,
               fail_on_stderr, kselftest=False):
    cp = configparser.SafeConfigParser(
            defaults={'kernel_part_uuid': None,
                      'root_uuid': None,
                      'defconfig': None,
                      'config_file': None,
                      'jobs': '1',
                      'vbutil_kernel': None,
                      'keyblock': None,
                      'data_key': None,
                      'cmdline': None,
                      'vbutil_arch': None,
                      'mkimage': None,
                      'its_file': None,
                      'completion_text': None,
                      'install_headers': 'no',
            }, allow_no_value=True)
    cp.read(ini_path)

    self.kernel_part_uuid = cp.get('target', 'kernel_part_uuid', raw=True)
    if self.kernel_part_uuid:
        self.kernel_part_uuid = self.kernel_part_uuid.lower()

    self.root_uuid = cp.get('target', 'root_uuid', raw=True)
    if self.root_uuid:
        self.root_uuid = self.root_uuid.lower()

    self.defconfig = cp.get('build', 'defconfig', raw=True)
    self.config_file = cp.get('build', 'config_file', raw=True)
    self.kernel_arch = cp.get('build', 'kernel_arch', raw=True)
    self.compiler = cp.get('build', 'compiler', raw=True)
    self.compiler_install = cp.get('build', 'compiler_install', raw=True)
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
    self.install_headers = cp.getboolean('build', 'install_headers')
    self.generate_htmldocs = cp.getboolean('build', 'generate_htmldocs')
    self.completion_text = cp.get('build', 'completion_text')

    self.generate_pkg = generate_pkg
    self.generate_compile_db = generate_compile_db
    self.fail_on_stderr = fail_on_stderr
    self.kselftest = kselftest

    if self.defconfig and self.config_file:
      raise ValueError('Specifying both defconfig and config_file is invalid')

    if self.defconfig:
      postfix = self.defconfig
    else:
      postfix = pathlib.PurePath(self.config_file).name

    if self.generate_htmldocs:
      prefix = 'htmldocs'
    elif self.kselftest:
      prefix = 'kselftest'
    else:
      prefix = 'build'

    self.output_path = pathlib.Path.cwd().joinpath(
                          '.{}_{}-{}'.format(prefix, self.kernel_arch, postfix))
    if not self.output_path.is_dir():
      self.output_path.mkdir()

    print('dc={} ps={} op={}'.format(self.defconfig, postfix, self.output_path))

    self.packed_kernel = self.output_path.joinpath('vmlinux.kpart')

  def __output_thread(self, pipe, output):
      for l in iter(pipe.readline, b''):
        line = l.rstrip().decode('utf-8')
        print(line)
        if output != None:
          output.append(line)


  def prompt_user(self, prompt):
    while True:
      reply = str(input(prompt + ' (y/n): ')).lower().strip()
      if reply[0] == 'y':
        return True
      if reply[0] == 'n':
        return False


  def __print_errors(self, prefix, errors, show_prompt):
    print('***********************************************************')
    print('*')
    if errors:
      print('*              {} WARNINGS/ERRORS'.format(prefix))
      print('*')
      for l in errors:
        print(l)
      print('*')
      print('***********************************************************')
      if show_prompt and not self.prompt_user('Would you like to continue?'):
        raise subprocess.CalledProcessError(1, args)
    else:
      print('*              {} BUILD IS CLEAN'.format(prefix))
      print('*')
      print('***********************************************************')

  def __run_command(self, args, fail_on_stderr=False, show_prompt=True):
    print('')
    print('#############################################################')
    print('#')
    print('# {}'.format(' '.join(args)))
    print('#')
    p = subprocess.Popen(args=args, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)

    stdout_thread = threading.Thread(target=self.__output_thread,
                                     args=(p.stdout, None))

    stderr = []
    stderr_thread = threading.Thread(target=self.__output_thread,
                                     args=(p.stderr, stderr))

    stdout_thread.start()
    stderr_thread.start()
    p.wait()
    stdout_thread.join()
    stderr_thread.join()

    drm_re = re.compile('(drivers/gpu/drm|include/drm|include/uapi/drm)')
    drm_stderr = []
    other_stderr = []
    for l in stderr:
      ignore = False
      for r in self.stderr_ignore:
        if r.search(l):
          print('IGNORE: {}'. format(l))
          ignore = True
          break
      if ignore:
        continue
      if drm_re.search(l):
        drm_stderr.append(l)
      else:
        other_stderr.append(l)

    self.__print_errors('DRM', drm_stderr, show_prompt)
    self.__print_errors('KERNEL', other_stderr, False)
    if p.returncode != 0:
      if not self.prompt_user('Build failed, would you like to continue?'):
        raise subprocess.CalledProcessError(p.returncode, args)


  def __run_make(self, flags=[], env={}, targets=[], root=False, bear=False):
    new_env = {}
    new_env['ARCH'] = self.kernel_arch
    new_env['O'] = str(self.output_path)
    #new_env['EXTRA_CFLAGS'] = '-Werror'
    new_env.update(env)

    try:
      old_env_compiler = os.environ.get('COMPILER')
      if old_env_compiler == None:
        old_env_compiler = ''
      os.environ['COMPILER'] = self.compiler

      old_env_compiler_path = os.environ.get('COMPILER_INSTALL_PATH')
      if old_env_compiler_path == None:
        old_env_compiler_path = ''
      os.environ['COMPILER_INSTALL_PATH'] = self.compiler_install

      args = []
      if root:
        args = ['sudo']
      if bear:
        args = args + ['bear']

      args = args + ['./make.cross']

      # kernel Makefile is inconsistent with which arguments can be set as env
      # variables, and which are cmdline assignments. So make all env cmdline
      # assignments
      for k,v in new_env.items():
        args.append('{}={}'.format(k, v))

      args.append('-j{}'.format(self.jobs))
      args = args + flags +  targets
      self.__run_command(args, fail_on_stderr=self.fail_on_stderr)
    finally:
      os.environ['COMPILER'] = old_env_compiler
      os.environ['COMPILER_INSTALL_PATH'] = old_env_compiler_path


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
    if (self.generate_pkg and
        not self.kselftest):
      self.__run_make(targets=['bindeb-pkg'])
    elif self.generate_htmldocs:
      self.__run_make(targets=['htmldocs'])
    elif self.kselftest:
      self.__run_make(targets=['kselftest'])
    else:
      self.__run_make(targets=['all'])

    if self.install_modules:
      modules_dst_path = self.output_path.joinpath('installed_modules')
      self.__run_make(env={ 'INSTALL_MOD_PATH': modules_dst_path },
                      targets=['modules_install'])
      print('Installed modules to {}'.format(modules_dst_path))

    if self.generate_compile_db:
        script_loc = 'scripts/gen_compile_commands.py'
        if not pathlib.Path(script_loc).exists():
            script_loc = 'scripts/clang-tools/gen_compile_commands.py'
        self.__run_command([script_loc,
                            '-d', str(self.output_path),
                            '--log_level', 'INFO'], fail_on_stderr=False,
                            show_prompt=False)

    if self.install_dtbs:
      self.__run_make(targets=['dtbs'])

    if self.install_headers:
        headers_dst_path = self.output_path.joinpath('headers')
        self.__run_make(env={ 'INSTALL_HDR_PATH': headers_dst_path },
                        targets=['headers_install'])



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
    try:
      repo = 'https://raw.githubusercontent.com/intel/lkp-tests/'
      url = repo + 'master/sbin/make.cross'
      urllib.request.urlretrieve(url, 'make.cross')

      st = os.stat('make.cross')
      os.chmod('make.cross', st.st_mode | stat.S_IEXEC)

      self.__configure()
      self.__make()

      if not self.kselftest:
          self.__package()
          self.__flash()

      if self.completion_text:
          print(self.completion_text)

    finally:
      #os.unlink('make.cross')
      print('Finished')


def main():
  parser = argparse.ArgumentParser(description='Build a kernel')
  parser.add_argument('--config', help='Optional build config path override',
                      action='append')
  parser.add_argument('--skip_gen_compile_db', default=False,
                      action='store_true',
                      help='Skip generating a compilation database')
  parser.add_argument('--gen_pkg', default=False, action='store_true',
                      help='Generate deb packages')
  parser.add_argument('--kselftest', default=False, action='store_true',
                      help='Do a kselftest build')
  parser.add_argument('--nofail_on_stderr', default=False, action='store_false',
                      help='Fail command on stderr')
  args = parser.parse_args()

  for c in args.config:
    builder = Builder(c, not args.skip_gen_compile_db, args.gen_pkg,
                      not args.nofail_on_stderr,
                      args.kselftest)
    builder.do_build()

if __name__ == '__main__':
  main()
