from os.path import basename, exists, join, splitext

import f90nml
from f90nml.namelist import Namelist
import pandas as pd
import re
from six import StringIO

from pymagicc import MAGICC6


class InputReader(object):
    header_tags = [
        "compiled by",
        "contact",
        "data",
        "date",
        "description",
        "gas",
        "source",
        "unit",
    ]

    def __init__(self, filename):
        self.filename = filename

    def _set_lines(self):
        with open(self.filename, 'r') as f:
            self.lines = f.readlines()

    def read(self):
        self._set_lines()
        # refactor to:
        # header, nml, data = self._get_split_lines()
        # metadata = self.process_metadata(header, nml)
        # df = self.process_data(data, metadata)
        # return metadata, df

        nml_end, nml_start = self._find_nml()

        nml_values = self.process_metadata(self.lines[nml_start : nml_end + 1])
        metadata = {key: value for key, value in nml_values.items() if key == "units"}
        metadata["header"] = "".join(self.lines[:nml_start])
        header_metadata = self.process_header(metadata["header"])
        metadata.update(header_metadata)

        # Create a stream from the remaining lines, ignoring any blank lines
        stream = StringIO()
        cleaned_lines = [l.strip() for l in self.lines[nml_end + 1 :] if l.strip()]
        stream.write("\n".join(cleaned_lines))
        stream.seek(0)

        df, metadata = self.process_data(stream, metadata)

        return metadata, df

    def _find_nml(self):
        """
        Find the start and end of the embedded namelist

        # Returns
        start, end (int): indexes for the namelist
        """
        nml_start = None
        nml_end = None
        for i in range(len(self.lines)):
            if self.lines[i].strip().startswith("&"):
                nml_start = i

            if self.lines[i].strip().startswith("/"):
                nml_end = i
        assert (
            nml_start is not None and nml_end is not None
        ), "Could not find namelist within {}".format(
            self.filename
        )
        return nml_end, nml_start

    def process_metadata(self, lines):
        # TODO: replace with f90nml.reads when released (>1.0.2)
        parser = f90nml.Parser()
        nml = parser._readstream(lines, {})
        metadata = {
            k.split("_")[1]: nml["THISFILE_SPECIFICATIONS"][k]
            for k in nml["THISFILE_SPECIFICATIONS"]
        }

        return metadata

    def process_data(self, stream, metadata):
        """
        Extract the tabulated data from a subset of the input file

        # Arguments
        stream (Streamlike object): A Streamlike object (nominally StringIO)
            containing the table to be extracted
        metadata (Dict): Dictionary containing

        # Returns
        return (Tuple): Tuple of a pd.DataFrame containing the data and a Dict
            containing the metadata. The pd.DataFrame columns are named using
            a MultiIndex
        """
        raise NotImplementedError()


    def process_header(self, header):
        """
        Parse the header for additional metadata

        The metadata is only present in MAGICC7 input files.
        :param header: A string containing all the lines in the header
        :return: A dict containing the addtional metadata in the header
        """
        metadata = {}
        for l in header.split("\n"):
            l = l.strip()
            for tag in self.header_tags:
                tag_text = "{}:".format(tag)
                if l.lower().startswith(tag_text):
                    metadata[tag] = l[len(tag_text) + 1 :].strip()
        return metadata

    def _read_data_header_line(self, stream, expected_header):
        tokens = stream.readline().split()
        assert tokens[0] == expected_header
        return tokens[1:]


class MAGICC6Reader(InputReader):
    def process_data(self, stream, metadata):
        df = pd.read_csv(
            stream,
            skip_blank_lines=True,
            delim_whitespace=True,
            engine="python")

        df.rename(columns={'COLCODE': 'YEAR'}, inplace=True)

        df = pd.melt(df, id_vars='YEAR', var_name='REGION', )

        df['UNITS'] = metadata['units']
        metadata.pop('units')

        df['TODO'] = 'SET'

        filename_only = splitext(basename(self.filename))[0]
        df['VARIABLE'] = '_'.join(filename_only.split('_')[1:])

        df.set_index(
            ['VARIABLE', 'TODO', 'REGION', 'YEAR', 'UNITS'],
            inplace=True
        )

        return df, metadata

        df.rename(columns={"COLCODE": "YEAR"}, inplace=True)

class MAGICC7Reader(InputReader):
    def process_data(self, stream, metadata):
        variables = self._read_data_header_line(stream, 'GAS')
        todo = self._read_data_header_line(stream, 'TODO')
        units = self._read_data_header_line(stream, 'UNITS')
        regions = self._read_data_header_line(stream, 'YEARS')  # Note that regions line starts with 'YEARS' instead of 'REGIONS'
        index = pd.MultiIndex.from_arrays(
            [variables, todo, regions, units],
            names=['VARIABLE', 'TODO', 'REGION', 'UNITS']
        )
        df = pd.read_csv(
            stream,
            skip_blank_lines=True,
            delim_whitespace=True,
            names=None,
            header=None,
            index_col=0,
        )
        df.index.name = "YEAR"
        df.columns = index
        df = df.T.stack()

        return df, metadata

    def _extract_units(self, gases, units):
        combos = set(zip(gases, units))
        result = {}
        for v, u in combos:
            if v not in result:
                result[v] = u
            else:
                # this isn't expected to happen, but should check anyway
                raise ValueError(
                    "Different units for {} in {}".format(v, self.filename)
                )

        return result

class CONC_INReader(InputReader):
    def process_data(self, stream, metadata):
        regions = self._read_data_header_line(stream, 'COLCODE') # Note that regions line starts with 'COLCODE' instead of 'REGIONS'
        units = [metadata['units']]*len(regions)
        metadata.pop('units')
        todo = ['SET']*len(regions)
        variables = [self._get_variable_from_filename()]*len(regions)
        index = pd.MultiIndex.from_arrays(
            [variables, todo, regions, units],
            names=['VARIABLE', 'TODO', 'REGION', 'UNITS']
        )
        df = pd.read_csv(
            stream,
            skip_blank_lines=True,
            delim_whitespace=True,
            names=None,
            header=None,
            index_col=0)
        df.index.name = 'YEAR'
        df.columns = index
        df = df.T.stack()

        return df, metadata

    def _get_variable_from_filename(self):
        regexp_capture_variable = re.compile(r'.*\_(\w*\_CONC)\.IN$')
        try:
            return regexp_capture_variable.search(self.filename).group(1)
        except AttributeError:
            error_msg = 'Cannot determine variable from filename: {}'.format(self.filename)
            raise SyntaxError(error_msg)


class HIST_EMIS_INReader(InputReader):
    def process_data(self, stream, metadata):
        if any(['COLCODE' in line for line in self.lines]):
            proxy_reader = MAGICC6Reader(self.filename)
        else:
            proxy_reader = MAGICC7Reader(self.filename)
        return proxy_reader.process_data(stream, metadata)

_file_types = {
    'MAGICC6': MAGICC6Reader,
    'MAGICC7': MAGICC7Reader,
}

_fname_reader_regex_map = {
    r'^HIST.*\_EMIS\.IN$': HIST_EMIS_INReader,
    # r'^.*\.SCEN$': SCENReader,
    # r'^.*\.SCEN7$': SCEN7Reader,
    r'^.*\_.*CONC.*\.IN$': CONC_INReader,
    # r'^INVERSEEMIS\_.*\.OUT$': INVERSEEMIS_OUTReader,
    # r'.*\.SECTOR$': SECTORReader,
}

def get_reader(fname):
    return determine_tool(fname, _fname_reader_regex_map)(fname)

    for fname_regex in _fname_reader_regex_map:
        if re.match(fname_regex, basename(fname)):
            return _fname_reader_regex_map[fname_regex](fname)

    # # Infer the file type from the header
    # if '.__  __          _____ _____ _____ _____   ______   ______ __  __ _____  _____  _____ _   _' \
    #         in lines[0]:
    #     file_type = 'MAGICC7'
    # else:
    #     file_type = 'MAGICC6'

    # return _file_types[file_type](fname, lines)


class MAGICCInput(object):
    """
    An interface to read and write the input files used by MAGICC.

    MAGICCInput can read input files from both MAGICC6 and MAGICC7. It returns
    files in a common format with a common vocabulary to simplify the process
    of reading, writing and handling MAGICC data.

    The MAGICCInput, once the target input file has been loaded, can be
    treated as a Pandas DataFrame. All the methods available to a DataFrame
    can be called on the MAGICCInput.

    ```python
    with MAGICC6() as magicc:
        mdata = MAGICCInput('HISTRCP_CO2I_EMIS.IN')
        mdata.read(magicc.run_dir)
        mdata.plot()
    ```

    TODO: Write example for writing

    # Parameters
    filename (str): Name of the file to read
    """

    def __init__(self, filename=None):
        """
        Initialise an Input file object.

        Optionally you can specify the filename of the target file. The file is
        not read until the search directory is provided in `read`. This allows
        for MAGICCInput files to be lazy-loaded once the appropriate MAGICC run
        directory is known.

        # Parameters
        filename (str): Optional file name, including extension, for the target
         file, i.e. 'HISTRCP_CO2I_EMIS.IN'
        """
        self.df = None
        self.metadata = {}
        self.name = filename

    def __getitem__(self, item):
        """
        Allow for indexing like a Pandas DataFrame

        >>> inpt = MAGICCInput('HISTRCP_CO2_CONC.IN')
        >>> inpt.read('./')
        >>> assert (inpt['CO2']['GLOBAL'] == inpt.df['CO2']['GLOBAL']).all()
        """
        if not self.is_loaded:
            self._raise_not_loaded_error()
        if len(item) == 2:
            return self.df['value'][item[0], :, item[1], :, :]
        elif len(item) == 3:
            return self.df['value'][item[0], :, item[1], item[2], :]


    def __getattr__(self, item):
        """
        Proxy any attributes/functions on the dataframe
        """
        if not self.is_loaded:
            self._raise_not_loaded_error()
        return getattr(self.df, item)

    def _raise_not_loaded_error(self):
        raise ValueError('File has not been read from disk yet')

    @property
    def is_loaded(self):
        return self.df is not None

    def read(self, filepath=None, filename=None):
        """
        Read an input file from disk

        # Parameters
        filepath (str): The directory to file the file from. This is often the
            run directory for a magicc instance. If None is passed,
            the run directory for the bundled version of MAGICC6 is used.
        filename (str): The filename to read. Overrides any existing values.
        """
        if filepath is None:
            filepath = MAGICC6().original_dir
        if filename is not None:
            self.name = filename
        assert self.name is not None
        filename = join(filepath, self.name)
        if not exists(filename):
            raise ValueError("Cannot find {}".format(filename))

        reader = get_reader(filename)
        self.metadata, self.df = reader.read()

    def write(self, filename):
        """
        TODO: Implement writing to disk
        """
        writer = get_writer(filename)
        writer.write(self, filename)
