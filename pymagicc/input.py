from os.path import basename, exists, join
from shutil import copyfileobj
from copy import deepcopy

import f90nml
from f90nml.namelist import Namelist
import pandas as pd
import re
from six import StringIO

from pymagicc import MAGICC6
from .definitions import (
    dattype_regionmode_regions,
    # Not used yet:
    # emissions_units,
    # concentrations_units,
)


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
        with open(self.filename, "r") as f:
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
        for line in header.split("\n"):
            line = line.strip()
            for tag in self.header_tags:
                tag_text = "{}:".format(tag)
                if line.lower().startswith(tag_text):
                    metadata[tag] = line[len(tag_text) + 1 :].strip()

        return metadata

    def _read_data_header_line(self, stream, expected_header):
        tokens = stream.readline().split()
        assert tokens[0] == expected_header
        return tokens[1:]


class HistConcInReader(InputReader):
    def process_data(self, stream, metadata):
        regions = self._read_data_header_line(
            stream, "COLCODE"
        )  # Note that regions line starts with 'COLCODE' instead of 'REGIONS'
        units = [metadata["units"]] * len(regions)
        metadata.pop("units")
        todos = ["SET"] * len(regions)
        variables = [self._get_variable_from_filename()] * len(regions)

        df = pd.read_csv(
            stream,
            skip_blank_lines=True,
            delim_whitespace=True,
            header=None,
            index_col=0,
        )
        df.index.name = "YEAR"
        df.columns = pd.MultiIndex.from_arrays(
            [variables, todos, units, regions],
            names=("VARIABLE", "TODO", "UNITS", "REGION"),
        )

        return df, metadata

    def _get_variable_from_filename(self):
        regexp_capture_variable = re.compile(r".*\_(\w*\_CONC)\.IN$")
        try:
            return regexp_capture_variable.search(self.filename).group(1)
        except AttributeError:
            error_msg = "Cannot determine variable from filename: {}".format(
                self.filename
            )
            raise SyntaxError(error_msg)


class HistEmisInReader(InputReader):
    def process_data(self, stream, metadata):
        if any(["COLCODE" in line for line in self.lines]):
            # Note that regions line starts with 'COLCODE' instead of 'REGIONS'
            regions = self._read_data_header_line(stream, "COLCODE")
            units = [metadata["units"]] * len(regions)
            metadata.pop("units")
            todos = ["SET"] * len(regions)
            variables = [self._get_variable_from_filename()] * len(regions)
        else:
            variables = self._read_data_header_line(stream, "GAS")
            todos = self._read_data_header_line(stream, "TODO")
            units = self._read_data_header_line(stream, "UNITS")
            metadata.pop("units")
            # Note that regions line starts with 'YEARS' instead of 'REGIONS'
            regions = self._read_data_header_line(stream, "YEARS")

        df = pd.read_csv(
            stream,
            skip_blank_lines=True,
            delim_whitespace=True,
            header=None,
            index_col=0,
        )
        df.index.name = "YEAR"
        df.columns = pd.MultiIndex.from_arrays(
            [variables, todos, units, regions],
            names=("VARIABLE", "TODO", "UNITS", "REGION"),
        )

        return df, metadata

    def _get_variable_from_filename(self):
        regexp_capture_variable = re.compile(r".*\_(\w*\_EMIS)\.IN$")
        try:
            return regexp_capture_variable.search(self.filename).group(1)
        except AttributeError:
            error_msg = "Cannot determine variable from filename: {}".format(
                self.filename
            )
            raise SyntaxError(error_msg)


class ScenReader(InputReader):
    def read(self):
        # annoyingly, will need to be a series of while loops:
        # - read header stuff
        # - read data
        # - read notes
        self._set_lines()
        self._stream = self._get_stream()
        header_notes_lines = self._read_header()
        df = self.read_data_block()
        header_notes_lines += self._read_notes()

        metadata = {"header": "".join(header_notes_lines)}
        metadata.update(self.process_header(metadata["header"]))

        return metadata, df

    def _get_stream(self):
        # Create a stream to work with, ignoring any blank lines
        stream = StringIO()
        cleaned_lines = [l.strip() for l in self.lines if l.strip()]
        stream.write("\n".join(cleaned_lines))
        stream.seek(0)

        return stream

    def _read_header(self):
        # I don't know how to do this without these nasty while True statements
        header_notes_lines = []
        while True:
            prev_pos = self._stream.tell()
            line = self._stream.readline()
            if not line:
                raise ValueError(
                    "Reached end of file without finding WORLD which should "
                    "always be the first region in a SCEN file"
                )

            if line.startswith("WORLD"):
                self._stream.seek(prev_pos)
                break

            header_notes_lines.append(line)

        return header_notes_lines

    def read_data_block(self):
        no_years = int(self.lines[0].strip())

        # go through datablocks until there are none left
        while True:
            try:
                pos_block = self._stream.tell()
                region = self._stream.readline().strip()

                variables = _convert_MAGICC6_to_MAGICC7_variables(
                    self._read_data_header_line(self._stream, "YEARS")
                )
                units = self._read_data_header_line(self._stream, "Yrs")
                todos = ["SET"] * len(variables)
                regions = [region] * len(variables)

                region_block = StringIO()
                for i in range(no_years):
                    region_block.write(self._stream.readline())
                region_block.seek(0)

                region_df = pd.read_csv(
                    region_block,
                    skip_blank_lines=True,
                    delim_whitespace=True,
                    header=None,
                    index_col=0,
                )
                region_df.index.name = "YEAR"
                region_df.columns = pd.MultiIndex.from_arrays(
                    [variables, todos, units, regions],
                    names=("VARIABLE", "TODO", "UNITS", "REGION"),
                )

                try:
                    df = df.join(region_df)
                except NameError:
                    df = region_df

            except IndexError:  # tried to get variables from empty string
                break
            except AssertionError:  # tried to get variables from a notes line
                break

        self._stream.seek(pos_block)

        return df

    def _read_notes(self):
        notes = []
        while True:
            line = self._stream.readline()
            if not line:
                break
            notes.append(line)

        return notes

def _convert_MAGICC6_to_MAGICC7_variables(variables, inverse=False):
    replacements = {
        "FossilCO2": "CO2I",
        "OtherCO2": "CO2B",
        "SOx": "SOX",
        "NOx": "NOX",
        "HFC43-10": "HFC4310",
        "HFC134a": "HFC134A",
        "HFC143a": "HFC143A",
        "HFC227ea": "HFC227EA",
        "HFC245fa": "HFC245FA",
    }
    variables_return = deepcopy(variables)
    for old, new in replacements.items():
        if inverse:
            old, new = (new, old)

        variables_return = [v.replace(old, new) for v in variables_return]

    return variables_return

def _convert_MAGICC7_to_MAGICC6_variables(variables):
    return _convert_MAGICC6_to_MAGICC7_variables(variables, inverse=True)

class InputWriter(object):
    def __init__(self):
        # need this for the _get_initial_nml_and_data_block routine as SCEN7
        # files have a special, contradictory set of region flags
        # would be nice to be able to remove in future
        self._scen_7 = False
        self._newline_char = "\n"
        pass

    def write(self, magicc_input, filename, filepath=None):
        """
        Write a MAGICC input file from df and metadata

        # Arguments
        magicc_input (MAGICCInput): a MAGICCInput object which holds the data
            to write
        filename (str): name of file to write to
        filepath (str): path in which to write file. If not provided,
           the file will be written in the current directory (TODO: check this is true...)
        """
        self.minput = magicc_input

        if filepath is not None:
            file_to_write = join(filepath, filename)
        else:
            file_to_write = filename

        output = StringIO()

        output = self._write_header(output)
        output = self._write_namelist(output)
        output = self._write_datablock(output)

        with open(file_to_write, "w") as output_file:
            output.seek(0)
            copyfileobj(output, output_file)

    def _write_header(self, output):
        output.write(self._get_header())
        return output

    def _get_header(self):
        return self.minput.metadata["header"]

    def _write_namelist(self, output):
        nml_initial, data_block = self._get_initial_nml_and_data_block()
        nml = nml_initial.copy()

        # '&NML_INDICATOR' goes above, '/'' goes at end
        no_lines_nml_header_end = 2
        line_after_nml = self._newline_char

        try:
            no_col_headers = len(data_block.columns.levels)
        except AttributeError:
            assert isinstance(data_block.columns, pd.core.indexes.base.Index)
            no_col_headers = 1

        nml["THISFILE_SPECIFICATIONS"]["THISFILE_FIRSTDATAROW"] = (
            len(output.getvalue().split(self._newline_char))
            + len(nml["THISFILE_SPECIFICATIONS"])
            + no_lines_nml_header_end
            + len(line_after_nml.split(self._newline_char))
            + no_col_headers
        )

        nml.uppercase = True
        nml._writestream(output)
        output.write(line_after_nml)

        return output

    def _write_datablock(self, output):
        nml_initial, data_block = self._get_initial_nml_and_data_block()

        # for most data files, as long as the data is space separated, the
        # format doesn't matter
        first_col_length = 12
        first_col_format_str = ("{" + ":{}d".format(first_col_length) + "}").format

        other_col_format_str = "{:18.5e}".format
        # I have no idea why these spaces are necessary at the moment, something wrong with pandas...?
        pd_pad = " " * (
            first_col_length - len(data_block.columns.get_level_values(0)[0]) - 1
        )
        output.write(pd_pad)
        formatters = [other_col_format_str] * len(data_block.columns)
        formatters[0] = first_col_format_str
        data_block.to_string(output, index=False, formatters=formatters, sparsify=False)
        output.write(self._newline_char)
        return output

    def _get_initial_nml_and_data_block(self):
        data_block = self._get_data_block()

        nml = Namelist()
        nml["THISFILE_SPECIFICATIONS"] = Namelist()
        nml["THISFILE_SPECIFICATIONS"]["THISFILE_DATACOLUMNS"] = (
            len(data_block.columns) - 1  # for YEARS column
        )
        nml["THISFILE_SPECIFICATIONS"]["THISFILE_FIRSTYEAR"] = data_block.iloc[0, 0]
        nml["THISFILE_SPECIFICATIONS"]["THISFILE_LASTYEAR"] = data_block.iloc[-1, 0]
        assert (
            (
                nml["THISFILE_SPECIFICATIONS"]["THISFILE_LASTYEAR"]
                - nml["THISFILE_SPECIFICATIONS"]["THISFILE_FIRSTYEAR"]
                + 1
            )
            / len(data_block)
            == 1.0
        )  # not ready for others yet
        nml["THISFILE_SPECIFICATIONS"]["THISFILE_ANNUALSTEPS"] = 1
        units_unique = list(set(self._get_df_header_row("UNITS")))
        assert len(units_unique) == 1  # again not ready for other stuff, write test before changing
        nml["THISFILE_SPECIFICATIONS"]["THISFILE_UNITS"] = units_unique[0]

        region_dattype_row = self._get_dattype_regionmode_regions_row()

        nml["THISFILE_SPECIFICATIONS"]["THISFILE_DATTYPE"] = dattype_regionmode_regions[
            "THISFILE_DATTYPE"
        ][region_dattype_row].iloc[0]
        nml["THISFILE_SPECIFICATIONS"][
            "THISFILE_REGIONMODE"
        ] = dattype_regionmode_regions["THISFILE_REGIONMODE"][region_dattype_row].iloc[
            0
        ]

        return nml, data_block

    def _get_data_block(self):
        raise NotImplementedError()

    def _get_dattype_regionmode_regions_row(self):
            regions_unique = set(self._get_df_header_row("REGION"))

            find_region = lambda x: set(x) == regions_unique
            region_rows = dattype_regionmode_regions["Regions"].apply(find_region)

            if self._scen_7:
                dattype_rows = dattype_regionmode_regions["THISFILE_DATTYPE"] == "SCEN7"
            else:
                dattype_rows = dattype_regionmode_regions["THISFILE_DATTYPE"] != "SCEN7"

            region_dattype_row = region_rows & dattype_rows
            assert sum(region_dattype_row) == 1
            return region_dattype_row

    def _get_df_header_row(self, col_name):
        return self.minput.df.columns.get_level_values(col_name).tolist()


class HistConcInWriter(InputWriter):
    def _get_data_block(self):
        regions = self._get_df_header_row("REGION")

        data_block = self.minput.df.copy().reset_index()
        data_block.columns = ["COLCODE"] + regions

        return data_block


class HistEmisInWriter(InputWriter):
    def _get_data_block(self):
        regions = self._get_df_header_row("REGION")
        variables = self._get_df_header_row("VARIABLE")
        units = self._get_df_header_row("UNITS")
        todos = self._get_df_header_row("TODO")

        data_block = self.minput.df.copy()
        # probably not necessary but a sensible check
        assert data_block.columns.names == ["VARIABLE", "TODO", "UNITS", "REGION"]
        data_block.reset_index(inplace=True)
        data_block.columns = [
            ["GAS"] + variables,
            ["TODO"] + todos,
            ["UNITS"] + units,
            ["YEARS"] + regions,
        ]

        return data_block


class ScenWriter(InputWriter):
    def _write_header(self, output):
        header_lines = []
        header_lines.append("{}".format(len(self.minput.df)))

        special_scen_code = self._get_special_scen_code(
            regions = self._get_df_header_row("REGION"),
            emissions = self._get_df_header_row("VARIABLE"),
        )
        header_lines.append("{}".format(special_scen_code))

        # for a scen file, the convention is (although all these lines are
        # actually ignored by source so could be anything):
        # - line 3 is name
        # - line 4 is description
        # - line 5 is notes (other notes lines go at the end)
        # - line 6 is empty
        header_lines.append("NAME - need better solution for how to control this")
        header_lines.append("DESCRIPTION - need better solution for how to control this")
        header_lines.append("NOTES - need better solution for how to control this")
        header_lines.append("")

        header_lines.append("OTHER NOTES - need better solution for how to control this")
        header_lines.append("OTHER NOTES - need better solution for how to control this")
        header_lines.append("OTHER NOTES - need better solution for how to control this")
        header_lines.append("OTHER NOTES - need better solution for how to control this")

        output.write(self._newline_char.join(header_lines))
        output.write(self._newline_char)

        return output

    def _get_special_scen_code(self, regions, emissions):
        valid_emissions = ['CO2I', 'CO2B', 'CH4', 'N2O', 'SOX', 'CO', 'NMVOC', 'NOX', 'BC', 'OC', 'NH3', 'CF4', 'C2F6', 'C6F14', 'HFC23', 'HFC32', 'HFC4310', 'HFC125', 'HFC134A', 'HFC143A', 'HFC227EA', 'HFC245FA', 'SF6']

        if set(valid_emissions) != set(emissions):
            msg = "Could not determine scen special code for emissions {}".format(
                emissions
            )
            raise ValueError(msg)

        if set(regions) == set(["WORLD"]):
            return 11
        elif set(regions) == set(["WORLD", "OECD90", "REF", "ASIA", "ALM"]):
            return 21
        elif set(regions) == set(["WORLD", "R5OECD", "R5REF", "R5ASIA", "R5MAF", "R5LAM"]):
            return 31
        elif set(regions) == set(["WORLD", "R5OECD", "R5REF", "R5ASIA", "R5MAF", "R5LAM", "BUNKERS"]):
            return 41

        msg = "Could not determine scen special code for regions {}".format(
                regions
            )
        raise ValueError(msg)


    def _write_namelist(self, output):
        # No namelist for SCEN files
        return output

    def _write_datablock(self, output):
        # for SCEN files, the data format is vitally important for the source code
        # we have to work out a better way of matching up all these conventions/testing them, tight coupling between pymagicc and MAGICC may solve it for us...
        lines = output.getvalue().split(self._newline_char)
        # notes are everything except the first 6 lines
        no_notes_lines = len(lines) - 6

        def _gip(lines, no_notes_lines):
            """
            get the point where we should insert the data block
            """
            return len(lines) - no_notes_lines

        region_order = self._get_region_order()
        # format is vitally important for SCEN files as far as I can tell
        first_col_length = 12
        first_col_format_str = ("{" + ":{}d".format(first_col_length) + "}").format
        other_col_format_str = "{:11.4f}".format

        # TODO: doing it this way, out of the loop,  should ensure things
        # explode if your regions don't all have the same number of emissions
        # timeseries or does extra timeseries in there (that probably
        # shouldn't raise an error, another one for the future), although the
        # explosion will be cryptic so should add a test for good error
        # message at some point
        formatters = (
            [other_col_format_str]
            * (
                int(len(self.minput.df.columns) / len(region_order))
                + 1  # for the years column
            )
        )
        formatters[0] = first_col_format_str

        for region in region_order:
            region_block = self.minput[region]
            region_block.columns = region_block.columns.droplevel("TODO")
            region_block.columns = region_block.columns.droplevel("REGION")
            variables = _convert_MAGICC7_to_MAGICC6_variables(
                region_block.columns.get_level_values("VARIABLE").tolist()
            )
            units = region_block.columns.get_level_values("UNITS").tolist()

            assert region_block.columns.names == ["VARIABLE", "UNITS"]
            region_block.reset_index(inplace=True)
            region_block.columns = [
                ["YEARS"] + variables,
                ["Yrs"] + units,
            ]

            # I have no idea why these spaces are necessary at the moment, something wrong with pandas...?
            pd_pad = " " * (
                first_col_length - len(self.minput.df.columns.get_level_values(0)[0]) - 2
            )
            region_block_str = region + self._newline_char
            region_block_str += pd_pad
            region_block_str += region_block.to_string(index=False, formatters=formatters, sparsify=False)
            region_block_str += self._newline_char * 2

            lines.insert(_gip(lines, no_notes_lines), region_block_str)

        output.seek(0)
        output.write(self._newline_char.join(lines))
        return output

    def _get_region_order(self):
        region_dattype_row = self._get_dattype_regionmode_regions_row()
        region_order = dattype_regionmode_regions["Regions"][region_dattype_row].iloc[0]

        return region_order


def determine_tool(fname, regexp_map, tool_to_find):
    for fname_regex in regexp_map:
        if re.match(fname_regex, basename(fname)):
            return regexp_map[fname_regex]

    raise ValueError("Couldn't find appropriate {} for {}".format(tool_to_find, fname))


hist_emis_in_regexp = r"^HIST.*\_EMIS\.IN$"
hist_conc_in_regexp = r"^.*\_.*CONC.*\.IN$"
scen_regexp = r"^.*\.SCEN$"
scen7_regexp = r"^.*\.SCEN7$"
inverse_emis_out_regexp = r"^INVERSEEMIS\_.*\.OUT$"
sector_regexp = r".*\.SECTOR$"

_fname_reader_regex_map = {
    scen_regexp: ScenReader,
    # scen7_regexp: Scen7Reader,
    # sector_regexp: SectorReader,
    hist_emis_in_regexp: HistEmisInReader,
    hist_conc_in_regexp: HistConcInReader,
    # inverse_emis_out_regexp: InverseEmisOutReader,
}


def _get_reader(fname):
    return determine_tool(fname, _fname_reader_regex_map, "Reader")(fname)


_fname_writer_regex_map = {
    scen_regexp: ScenWriter,
    # scen7_regexp: Scen7Writer,
    # sector_regexp: SectorWriter,
    hist_emis_in_regexp: HistEmisInWriter,
    hist_conc_in_regexp: HistConcInWriter,
    # inverse_emis_out_regexp: InverseEmisOutWriter,
}


def _get_writer(fname):
    return determine_tool(fname, _fname_writer_regex_map, "Writer")()


def _get_df_key(df, key):
    for colname in df.columns.names:
        try:
            return df.xs(key, level=colname, axis=1, drop_level=False)
        except KeyError:
            continue
    try:
        return df.loc[[key]]
    except KeyError:
        msg = "{} is not in the column headers or the index".format(key)
        raise KeyError(msg)


def _get_df_keys(df, keys):
    df_out = df.copy()
    if isinstance(keys, str):
        keys = [keys]
    try:
        for key in keys:
            df_out = _get_df_key(df_out, key)
    except TypeError:
        df_out = _get_df_key(df_out, keys)
    return df_out


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
        self.filename = filename

    def __getitem__(self, item):
        """
        Allow for simplified indexing

        # TODO: double check of delete below
        >>> inpt = MAGICCInput('HISTRCP_CO2_CONC.IN')
        >>> inpt.read('./')
        >>> assert (inpt['CO2', 'GLOBAL'] == inpt.df['CO2', :, :, 'GLOBAL']).all()
        """
        if not self.is_loaded:
            self._raise_not_loaded_error()
        return _get_df_keys(self.df, item)

    def __getattr__(self, item):
        """
        Proxy any attributes/functions on the dataframe
        """
        if not self.is_loaded:
            self._raise_not_loaded_error()
        return getattr(self.df, item)

    def _raise_not_loaded_error(self):
        raise ValueError("File has not been read from disk yet")

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
            self.filename = filename
        assert self.filename is not None

        file_to_read = join(filepath, self.filename)
        if not exists(file_to_read):
            raise ValueError("Cannot find {}".format(file_to_read))

        reader = _get_reader(file_to_read)
        self.metadata, self.df = reader.read()

    def write(self, filename, filepath=None):
        """
        TODO: Implement writing to disk
        """
        writer = _get_writer(filename)
        writer.write(self, filename, filepath)
