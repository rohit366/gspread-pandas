from __future__ import print_function

from re import match

from builtins import str, range, super
from past.builtins import basestring

import numpy as np
import pandas as pd

from decorator import decorator

from oauth2client.client import OAuth2Credentials

from gspread.models import Worksheet
from gspread.exceptions import (SpreadsheetNotFound, WorksheetNotFound,
                                NoValidUrlKeyFound)
from gspread.exceptions import APIError
from gspread.client import Client as ClientV4
from gspread_pandas.conf import get_creds, default_scope
from gspread_pandas.exceptions import (GspreadPandasException, NoWorksheetException,
                                MissMatchException)
from gspread_pandas.util import (chunks, parse_df_col_names,
                                 parse_sheet_index, parse_sheet_headers,
                                 create_frozen_request, create_filter_request,
                                 fillna, get_cell_as_tuple, get_range)


__all__ = ['Spread', 'Client']

ROW = 0
COL = 1

class Client(ClientV4):
    """
    The gspread_pandas :class:`Client` extends :class:`Client <gspread.client.Client>`
    and authenticates using credentials stored in ``gspread_pandas`` config.

    This class also adds a few convenience methods to explore the user's google drive
    for spreadsheets.
    """
    _email = None

    def __init__(self, user_or_creds, config=None, scope=default_scope):
        """
        :param str user_or_creds: string indicating the key to a users credentials,
            which will be stored in a file (by default they will be stored in
            ``~/.config/gspread_pandas/creds/<user>`` but can be modified with
            ``creds_dir`` property in config) or an instance of
            :class:`OAuth2Credentials <oauth2client.client.OAuth2Credentials>`
        :param dict config: optional, if you want to provide an alternate configuration,
            see :meth:`get_config <gspread_pandas.conf.get_config>`
        :param list scope: optional, if you'd like to provide your own scope
        """
        #: `(list)` - Feeds included for the OAuth2 scope
        self.scope = scope
        self._login(user_or_creds, config)

    def _login(self, user_or_creds, config):
        if isinstance(user_or_creds, OAuth2Credentials):
            creds = user_or_creds
        elif isinstance(user_or_creds, basestring):
            creds = get_creds(user_or_creds, config, self.scope)
        else:
            raise TypeError('user_or_client needs to be a string '
                            'or OAuth2Credentials object')

        super().__init__(creds)
        super().login()

    @decorator
    def _ensure_auth(func, self, *args, **kwargs):
        self.login()
        return func(self, *args, **kwargs)

    def get_email(self):
        """Return the email address of the user"""
        if not self._email:
            try:
                self._email = self.request('get',
                                           'https://www.googleapis.com/userinfo/v2/me')\
                                  .json()['email']
            except Exception:
                print("""
                Couldn't retrieve email. Delete credentials and authenticate again
                """)

        return self._email

    @_ensure_auth
    def _make_drive_request(self, q):
        files = []
        page_token = ''
        url = "https://www.googleapis.com/drive/v3/files"
        params = {
            'q': q,
            "pageSize": 1000
        }

        while page_token is not None:
            if page_token:
                params['pageToken'] = page_token

            res = self.request('get', url, params=params).json()
            files.extend(res['files'])
            page_token = res.get('nextPageToken', None)

        return files

    def list_spreadsheet_files(self):
        """Return all spreadsheets that the user has access to"""
        q = "mimeType='application/vnd.google-apps.spreadsheet'"
        return self._make_drive_request(q)

    def list_spreadsheet_files_in_folder(self, folder_id):
        """Return all spreadsheets that the user has access to in a sepcific folder.

        :param str folder_id: ID of a folder, see :meth:`find_folders <find_folders>`
        """
        q = ("mimeType='application/vnd.google-apps.spreadsheet'"
             " and '{0}' in parents".format(folder_id))

        return self._make_drive_request(q)

    def find_folders(self, folder_name_query):
        """Return all folders that the user has access to containing
        ``folder_name_query`` in the name

        :param str folder_name_query: Case insensitive string to search in folder name
        """
        q = ("mimeType='application/vnd.google-apps.folder'"
             " and name contains '{0}'".format(folder_name_query))

        return self._make_drive_request(q)

    def find_spreadsheet_files_in_folders(self, folder_name_query):
        """Return all spreadsheets that the user has access to in all the folders that
        contain ``folder_name_query`` in the name. Returns as a dict with each key being
        the folder name and the value being a list of spreadsheet files

        :param str folder_name_query: Case insensitive string to search in folder name
        """
        results = {}
        for res in self.find_folders(folder_name_query):
            results[res['name']] = self.list_spreadsheet_files_in_folder(res['id'])
        return results


class Spread():
    """
    Simple wrapper for gspread to interact with Pandas. It holds an instance of
    an 'open' spreadsheet, an 'open' worksheet, and a list of available worksheets.

    Each user will be associated with specific OAuth credentials. The authenticated user
    will need the appropriate permissions to the Spreadsheet in order to interact with it.
    """
    #: `(gspread.models.Spreadsheet)` - Currently open Spreadsheet
    spread = None

    #: `(gspread.models.Worksheet)` - Currently open Worksheet
    sheet = None

    #: `(Client)` - Instance of gspread_pandas :class:`Client <gspread_pandas.client.Client>`
    client = None

    # chunk range request: https://github.com/burnash/gspread/issues/375
    _max_range_chunk_size = 1000000

    # `(dict)` - Spreadsheet metadata
    _spread_metadata = None

    def __init__(self, user_creds_or_client, spread, sheet=None, config=None,
                 create_spread=False, create_sheet=False, scope=default_scope):
        """
        :param str user_creds_or_client: string indicating the key to a users credentials,
            which will be stored in a file (by default they will be stored in
            ``~/.config/gspread_pandas/creds/<user>`` but can be modified with ``creds_dir``
            property in config) or an instance of a
            :class:`Client <gspread_pandas.client.Client>`
        :param str spread: name, url, or id of the spreadsheet; must have read access by
            the authenticated user,
            see :meth:`open_spread <gspread_pandas.client.Spread.open_spread>`
        :param str,int sheet: optional, name or index of Worksheet,
            see :meth:`open_sheet <gspread_pandas.client.Spread.open_sheet>` (default None)
        :param dict config: optional, if you want to provide an alternate configuration,
            see :meth:`get_config <gspread_pandas.conf.get_config>`
        :param bool create_sheet: whether to create the spreadsheet if it doesn't exist,
            it wil use the ``spread`` value as the sheet title
        :param bool create_spread: whether to create the sheet if it doesn't exist,
            it wil use the ``spread`` value as the sheet title
        :param list scope: optional, if you'd like to provide your own scope
        """
        if isinstance(user_creds_or_client, Client):
            self.client = user_creds_or_client
        elif isinstance(user_creds_or_client, (basestring, OAuth2Credentials)):
            self.client = Client(user_creds_or_client, config, scope)
        else:
            raise TypeError('user_creds_or_client needs to be a string, '
                            'OAuth2Credentials, or Client object')

        self.open(spread, sheet, create_sheet, create_spread)

    def __repr__(self):
        base = "<gspread_pandas.client.Spread - '{0}'>"
        meta = []
        if self.email:
            meta.append("User: '{0}'".format(self.email))
        if self.spread:
            meta.append("Spread: '{0}'".format(self.spread.title))
        if self.sheet:
            meta.append("Sheet: '{0}'".format(self.sheet.title))
        return base.format(", ".join(meta))

    @property
    def email(self):
        """`(str)` - E-mail for the currently authenticated user"""
        return self.client.get_email()

    @property
    def url(self):
        """`(str)` - Url for this spreadsheet"""
        return 'https://docs.google.com/spreadsheets/d/{0}'.format(self.spread.id)

    @property
    def sheets(self):
        """`(list)` - List of available Worksheets"""
        return self.spread.worksheets()

    def refresh_spread_metadata(self):
        """Refresh spreadsheet metadata"""
        self._spread_metadata = self.spread.fetch_sheet_metadata()

    @property
    def _sheet_metadata(self):
        """`(dict)` - Metadata for currently open worksheet"""
        if self.sheet:
            ix = self._find_sheet(self.sheet.title)[0]
            return self._spread_metadata['sheets'][ix]

    @decorator
    def _ensure_auth(func, self, *args, **kwargs):
        self.client.login()
        return func(self, *args, **kwargs)

    def open(self, spread, sheet=None, create_sheet=False, create_spread=False):
        """
        Open a spreadsheet, and optionally a worksheet. See
        :meth:`open_spread <gspread_pandas.Spread.open_spread>` and
        :meth:`open_sheet <gspread_pandas.Spread.open_sheet>`.

        :param str spread: name, url, or id of Spreadsheet
        :param str,int sheet: name or index of Worksheet
        :param bool create_sheet: whether to create the spreadsheet if it doesn't exist,
            it wil use the ``spread`` value as the sheet title
        :param bool create_spread: whether to create the sheet if it doesn't exist,
            it wil use the ``spread`` value as the sheet title
        """
        self.open_spread(spread, create_spread)

        if sheet is not None:
            self.open_sheet(sheet, create_sheet)

    @_ensure_auth
    def open_spread(self, spread, create=False):
        """
        Open a spreadsheet. Authorized user must already have read access.

        :param str spread: name, url, or id of Spreadsheet
        :param bool create: whether to create the spreadsheet if it doesn't exist,
            it wil use the ``spread`` value as the sheet title
        """
        id_regex = "[a-zA-Z0-9-_]{44}"
        url_path = "docs.google.com/spreadsheet"

        if match(id_regex, spread):
            open_func = self.client.open_by_key
        elif url_path in spread:
            open_func = self.client.open_by_url
        else:
            open_func = self.client.open

        try:
            self.spread = open_func(spread)
            self.refresh_spread_metadata()
        except (SpreadsheetNotFound, NoValidUrlKeyFound, APIError):
            if create:
                try:
                    self.spread = self.client.create(spread)
                    self.refresh_spread_metadata()
                except Exception as e:
                    msg = "Couldn't create spreadsheet.\n" + str(e)
                    raise GspreadPandasException(msg)
            else:
                raise SpreadsheetNotFound("Spreadsheet not found")

    @_ensure_auth
    def open_sheet(self, sheet, create=False):
        """
        Open a worksheet. Optionally, if the sheet doesn't exist then create it first
        (only when ``sheet`` is a str).

        :param str,int,Worksheet sheet: name, index, or Worksheet object
        :param bool create: whether to create the sheet if it doesn't exist,
            see :meth:`create_sheet <gspread_pandas.Spread.create_sheet>` (default False)
        """
        self.sheet = None
        if isinstance(sheet, int):
            try:
                self.sheet = self.sheets[sheet]
            except Exception:
                raise WorksheetNotFound("Invalid sheet index {0}".format(sheet))
        else:
            self.sheet = self.find_sheet(sheet)

        if not self.sheet:
            if create:
                self.create_sheet(sheet)
            else:
                raise WorksheetNotFound("Worksheet not found")

    @_ensure_auth
    def create_sheet(self, name, rows=1, cols=1):
        """
        Create a new worksheet with the given number of rows and cols.

        Automatically opens that sheet after it's created.

        :param str name: name of new Worksheet
        :param int rows: number of rows (default 1)
        :param int cols: number of columns (default 1)
        """
        self.spread.add_worksheet(name, rows, cols)
        self.refresh_spread_metadata()
        self.open_sheet(name)

    @_ensure_auth
    def sheet_to_df(self, index=1, header_rows=1, start_row=1, sheet=None):
        """
        Pull a worksheet into a DataFrame.

        :param int index: col number of index column, 0 or None for no index (default 1)
        :param int header_rows: number of rows that represent headers (default 1)
        :param int start_row: row number for first row of headers or data (default 1)
        :param str,int sheet: optional, if you want to open a different sheet first,
            see :meth:`open_sheet <gspread_pandas.client.Spread.open_sheet>` (default None)

        :returns: a DataFrame with the data from the Worksheet
        """
        if sheet:
            self.open_sheet(sheet)

        if not self.sheet:
            raise NoWorksheetException("No open worksheet")

        vals = self._retry_get_all_values()
        vals = self._fix_merge_values(vals)[start_row - 1:]

        col_names = parse_sheet_headers(vals, header_rows)

        # remove rows where everything is null, then replace nulls with ''
        df = pd.DataFrame(vals[header_rows or 0:])\
               .replace('', np.nan)\
               .dropna(how='all')\
               .fillna('')

        if col_names is not None:
            if len(df.columns) == len(col_names):
                df.columns = col_names
            elif len(df) == 0:
                # if we have headers but no data, set column headers on empty DF
                df = df.reindex(columns=col_names)
            else:
                raise MissMatchException("Column headers don't match number of data columns")

        return parse_sheet_index(df, index)

    @_ensure_auth
    def get_sheet_dims(self, sheet=None):
        """
        Get the dimensions of the currently open Worksheet.

        :param str,int,Worksheet sheet: optional, if you want to open a different sheet first,
            see :meth:`open_sheet <gspread_pandas.client.Spread.open_sheet>` (default None)

        :returns: a tuple containing (num_rows,num_cols)
        """
        if sheet:
            self.open_sheet(sheet)

        return (self.sheet.row_count,
                self.sheet.col_count) if self.sheet else None


    def _get_update_chunks(self, start, end, vals):
        start = get_cell_as_tuple(start)
        end = get_cell_as_tuple(end)

        num_cols = end[COL] - start[COL] + 1
        num_rows = end[ROW] - start[ROW] + 1
        num_cells = num_cols * num_rows

        if num_cells != len(vals):
            raise MissMatchException("Number of values needs to match number of cells")

        chunk_rows = self._max_range_chunk_size // num_cols
        chunk_size = chunk_rows * num_cols

        end_cell = (start[ROW] - 1, 0)

        for val_chunks in chunks(vals, int(chunk_size)):
            start_cell = (end_cell[ROW] + 1,
                          start[COL])
            end_cell = (min(start_cell[ROW] + chunk_rows - 1,
                            start[ROW] + num_rows - 1),
                        end[COL])
            yield start_cell, end_cell, val_chunks

    @_ensure_auth
    def update_cells(self, start, end, vals, sheet=None):
        """
        Update the values in a given range. The values should be listed in order
        from left to right across rows.

        :param tuple,str start: tuple indicating (row, col) or string like 'A1'
        :param tuple,str end: tuple indicating (row, col) or string like 'Z20'
        :param list vals: array of values to populate
        :param str,int,Worksheet sheet: optional, if you want to open a different sheet first,
            see :meth:`open_sheet <gspread_pandas.client.Spread.open_sheet>` (default None)
        """
        if sheet:
            self.open_sheet(sheet)

        if not self.sheet:
            raise NoWorksheetException("No open worksheet")

        if start == end:
            return

        for start_cell, end_cell, val_chunks in self._get_update_chunks(start,
                                                                        end,
                                                                        vals):
            rng = get_range(start_cell, end_cell)

            cells = self._retry_range(rng)

            if len(val_chunks) != len(cells):
                raise MissMatchException("Number of chunked values doesn't match number of cells")

            for val, cell in zip(val_chunks, cells):
                cell.value = val

            self._retry_update(cells)

    @_ensure_auth
    def _retry_get_all_values(self, n=3):
        """Call self.sheet.update_cells with retry"""
        try:
            return self.sheet.get_all_values()
        except Exception as e:
            if n > 0:
                self._retry_get_all_values(n-1)
            else:
                raise e

    @_ensure_auth
    def _retry_update(self, cells, n=3):
        """Call self.sheet.update_cells with retry"""
        try:
            self.sheet.update_cells(cells, 'USER_ENTERED')
        except Exception as e:
            if n > 0:
                self._retry_update(cells, n-1)
            else:
                raise e

    @_ensure_auth
    def _retry_range(self, rng, n=3):
        """Call self.sheet.range with retry"""
        try:
            return self.sheet.range(rng)
        except Exception as e:
            if n > 0:
                self._retry_range(rng, n-1)
            else:
                raise e

    def _find_sheet(self, sheet):
        """Find a worksheet and return with index"""
        for ix, worksheet in enumerate(self.sheets):
            if isinstance(sheet, basestring) and sheet.lower() == worksheet.title.lower():
                return ix, worksheet
            if isinstance(sheet, Worksheet) and sheet == worksheet:
                return ix, worksheet
        return None, None

    def find_sheet(self, sheet):
        """
        Find a given worksheet by title

        :param str sheet: name of Worksheet

        :returns: a Worksheet by the given name or None if not found
        """
        return self._find_sheet(sheet)[1]

    @_ensure_auth
    def clear_sheet(self, rows=1, cols=1, sheet=None):
        """
        Reset open worksheet to a blank sheet with given dimensions.

        :param int rows: number of rows (default 1)
        :param int cols: number of columns (default 1)
        :param str,int,Worksheet sheet: optional; name, index, or Worksheet,
            see :meth:`open_sheet <gspread_pandas.client.Spread.open_sheet>` (default None)
        """
        if sheet:
            self.open_sheet(sheet)

        if not self.sheet:
            raise NoWorksheetException("No open worksheet")

        self.sheet.resize(rows, cols)

        self.update_cells(
            start=(1, 1),
            end=(rows, cols),
            vals=['' for i in range(0, rows * cols)]
        )

    @_ensure_auth
    def delete_sheet(self, sheet):
        """
        Delete a worksheet by title. Returns whether the sheet was deleted or not. If
        current sheet is deleted, the ``sheet`` property will be set to None.

        :param str,Worksheet sheet: name or Worksheet

        :returns: True if deleted successfully, else False
        """
        is_current = False

        s = self.find_sheet(sheet)

        if s == self.sheet:
            is_current = True

        if s:
            try:
                self.spread.del_worksheet(s)
                if is_current:
                    self.sheet = None
                return True
            except Exception:
                pass

        self.refresh_spread_metadata()

        return False

    @_ensure_auth
    def df_to_sheet(self, df, index=True, headers=True, start=(1,1), replace=False,
                    sheet=None, freeze_index=False, freeze_headers=False, fill_value='',
                    add_filter=False):
        """
        Save a DataFrame into a worksheet.

        :param DataFrame df: the DataFrame to save
        :param bool index: whether to include the index in worksheet (default True)
        :param bool headers: whether to include the headers in the worksheet (default True)
        :param tuple,str start: tuple indicating (row, col) or string like 'A1' for top left
            cell
        :param bool replace: whether to remove everything in the sheet first (default False)
        :param str,int,Worksheet sheet: optional, if you want to open or create a different sheet
            before saving,
            see :meth:`open_sheet <gspread_pandas.client.Spread.open_sheet>` (default None)
        :param bool freeze_index: whether to freeze the index columns (default False)
        :param bool freeze_headers: whether to freeze the header rows (default False)
        :param str fill_value: value to fill nulls with (default '')
        :param bool add_filter: whether to add a filter to the uploaded sheet (default
            False)
        """
        if sheet:
            self.open_sheet(sheet, create=True)

        if not self.sheet:
            raise NoWorksheetException("No open worksheet")

        index_size = df.index.nlevels
        header_size = df.columns.nlevels

        if index:
            df = df.reset_index()

        df = fillna(df, fill_value)
        df_list = df.values.tolist()

        if headers:
            header_rows = parse_df_col_names(df, index, index_size)
            df_list = header_rows + df_list

        start = get_cell_as_tuple(start)

        sheet_rows, sheet_cols = self.get_sheet_dims()
        req_rows = len(df_list) + (start[ROW] - 1)
        req_cols = len(df_list[0]) + (start[COL] - 1) or 1

        if replace:
            # this takes care of resizing
            self.clear_sheet(req_rows, req_cols)
        else:
            # make sure sheet is large enough
            self.sheet.resize(max(sheet_rows, req_rows), max(sheet_cols, req_cols))

        self.update_cells(
            start=start,
            end=(req_rows, req_cols),
            vals=[str(val) for row in df_list for val in row]
        )

        self.freeze(None if not freeze_headers else header_size,
                    None if not freeze_index else index_size)

        if add_filter:
            self.add_filter(header_size + start[0] - 2, req_rows,
                            start[1] - 1, req_cols)

    def _fix_merge_values(self, vals):
        """Assign the top-left value to all cells in a merged range"""
        for merge in self._sheet_metadata.get('merges', []):
            start_row, end_row = merge['startRowIndex'], merge['endRowIndex']
            start_col, end_col = merge['startColumnIndex'], merge['endColumnIndex']

            # ignore merge cells outside the data range
            if start_row < len(vals) and start_col < len(vals[0]):
                orig_val = vals[start_row][start_col]
                for row in vals[start_row:end_row]:
                    row[start_col:end_col] = [orig_val for i in
                                              range(start_col, end_col)]

        return vals

    @_ensure_auth
    def freeze(self, rows=None, cols=None, sheet=None):
        """
        Freeze rows and/or columns for the open worksheet.

        :param int rows: the DataFrame to save
        :param int cols: whether to include the index in worksheet (default True)
        :param str,int,Worksheet sheet: optional, if you want to open or create a
            different sheet before freezing,
            see :meth:`open_sheet <gspread_pandas.client.Spread.open_sheet>`
            (default None)
        """
        if sheet:
            self.open_sheet(sheet, create=True)

        if not self.sheet:
            raise NoWorksheetException("No open worksheet")

        if rows is None and cols is None:
            return

        self.spread.batch_update({
            'requests': create_frozen_request(self.sheet.id, rows, cols)
        })

        self.refresh_spread_metadata()

    @_ensure_auth
    def add_filter(self, start_row=None, end_row=None, start_col=None,
                   end_col=None, sheet=None):
        """
        Add filters to data in the open worksheet.

        :param int start_row: First row to include in filter; this will be the
            filter header (Default 0)
        :param int end_row: Last row to include in filter (Default last row in sheet)
        :param int start_col: First column to include in filter (Default 0)
        :param int end_col: Last column to include in filter (Default last column
            in sheet)
        :param str,int,Worksheet sheet: optional, if you want to open or create a
            different sheet before adding the filter,
            see :meth:`open_sheet <gspread_pandas.client.Spread.open_sheet>`
            (default None)
        """
        if sheet:
            self.open_sheet(sheet, create=True)

        if not self.sheet:
            raise NoWorksheetException("No open worksheet")

        dims = self.get_sheet_dims()

        self.spread.batch_update({
            'requests': create_filter_request(
                self.sheet.id,
                start_row or 0,
                end_row or dims[0],
                start_col or 0,
                end_col or dims[1]
            )
        })
