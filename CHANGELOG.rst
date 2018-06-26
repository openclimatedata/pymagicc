Unreleased
==========

- drops support for Python 2
- RCP3PD and RCP6 to RCP26 and RCP60 for consistency and MAGICC7 compatibility
- add `--skip-slow` option to tests

1.1.0
=====

- add reading of `MAGICC_EXECUTABLE` environment variable to simplify setting
  path of MAGICC package for testing and CI (thanks @lewisjared)

1.0.2
=====

- interactive demo Notebook using Jupyter Notebook's `appmode` extension
- documentation improvements

1.0.1
====

- Un-pin `f90nml` dependency, 0.23 is working with Pymagicc again

1.0.0
=====

- API Stable release

0.9.3
=====

- workaround for bug in Pandas (https://github.com/pandas-dev/pandas/issues/18692)
  when reading some files from alternative MAGICC builds
- improve documentation

0.9.2
=====

- add Windows testing and fix running on Windows
- simplify configuration by only having optional config parameters

0.8.0
=====

- pin f90nml version because later release breaks with MAGICC output


0.7.0
=====

- switch to Dictionaries as results object and scenarios data structure
  since Pandas `panel` is being deprecated.

0.6.4
=====

- returning used parameters in MAGICC ``run`` function is optional
- fix versioning for PyPI installs

0.4
===

Initial release.
