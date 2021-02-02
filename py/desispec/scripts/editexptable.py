
from desispec.workflow.exptable import get_exposure_table_name,get_exposure_table_path, \
                                       get_exposure_flags, get_exposure_table_column_defs, keyval_change_reporting
from desispec.workflow.tableio import load_table, write_table
from desispec.workflow.utils import pathjoin

import os
import numpy as np
from astropy.table import Table

def process_int_range(input_string):
    for symbol in [':','-','..']:
        if symbol in input_string:
            first,last = input_string.split(symbol)
            return np.arange(int(first),int(last)+1)

def parse_int_list_term(input_string, intable=None):
    if input_string.lower() == 'all' and intable is not None:
        out_array = intable['EXPID'].data
    elif input_string.isnumeric():
        out_array = np.atleast_1d(int(input_string))
    elif np.any([symb in input_string for symb in [':','-','..']]):
        out_array = process_int_range(input_string)
    else:
        raise ValueError(f"Couldn't understand input {input_string}")
    return out_array

def parse_int_list(input_string, intable=None, only_unique=True):
    input_string = input_string.strip(' \t,')
    out_array = np.atleast_1d()
    for substr in input_string.split(","):
        out_array = np.append(out_array, parse_int_list_term(substr, intable=intable))
    if only_unique:
        out_array = np.unique(out_array)
    return out_array.astype(int)

def change_exposure_table_rows(exptable, exp_str, colname, value, append_value=True, overwrite_value=False, joinsymb=';'):
    ## Make sure colname exists before proceeding
    ## Don't edit fixed columns
    colname = colname.upper()
    if colname in ['EXPID','CAMWORD']:
        raise ValueError(f"Not allowed to edit colname={colname}.")
    if colname not in exptable.colnames:
        raise ValueError(f"Colname {colname} not in exposure table")

    ## Parse the exposure numbers
    exposure_list = parse_int_list(exp_str, intable=exptable, only_unique=True)
    print(f"Changing column: {colname} values to {value} for exposures: {exposure_list}.")

    ## Match exposures to row numbers
    row_numbers = []
    for exp in exposure_list:
        rownum = np.where(exptable['EXPID'] == exp)[0]
        if rownum.size > 0:
            row_numbers.append(rownum[0])
    row_numbers = np.asarray(row_numbers)

    ## Match data type and convert where necessary
    if colname == 'EXPFLAG':
        expflags = get_exposure_flags()
        value = value.lower().replace(' ','_')
        if value not in expflags:
            raise ValueError(f"Couldn't understand exposure flag: {value}")
    elif colname == 'BADAMPS':
        value = value.replace(' ','')
        for symb in [',',':','|','.']:
            value = value.replace(symb,joinsymb)
        for amp in value.split(joinsymb):
            if len(amp)!=3 or not amp[1].isnumeric():
                raise ValueError("Each BADAMPS entry must be a semicolon separated list of {camera}{petal}{amp} "+
                                 f"(e.g. r7A;b8B). Given: {amp}")

    ## Get column names and definitions
    colnames,coldtypes,coldeflts = get_exposure_table_column_defs(return_default_values=True)
    colnames,coldtypes,coldeflts = np.array(colnames),np.array(coldtypes),np.array(coldeflts,dtype=object)
    cur_dtype = coldtypes[colnames==colname][0]
    cur_default = coldeflts[colnames==colname][0]

    ## Assign new value
    isstr = (cur_dtype in [str, np.str, np.str_] or type(cur_dtype) is str)
    isarr = (cur_dtype in [list, np.array, np.ndarray])
    for rownum in row_numbers:
        if isstr and str(exptable[colname][rownum]).strip() != '':
            if append_value:
                exptable[colname][rownum] += f'{joinsymb}{value}'
            elif overwrite_value:
                exptable[rownum] = document_in_comments(exptable[rownum],colname,value)
                exptable[colname][rownum] = f'{value}'
            else:
                exp = exptable[rownum]['EXPID']
                raise ValueError \
                    (f"In exposure: {exp}. Asked to overwrite non-empty cell without overwrite_value or append_value enabled Skipping.")
        elif isarr and len(exptable[colname][rownum])>0:
            if append_value:
                exptable[colname][rownum] = np.append(exptable[colname][rownum], value)
            elif overwrite_value:
                exptable[rownum] = document_in_comments(exptable[rownum],colname,value)
                exptable[colname][rownum] = np.append(cur_default, value)
            else:
                exp = exptable[rownum]['EXPID']
                raise ValueError \
                    (f"In exposure: {exp}. Asked to overwrite non-empty cell without overwrite_value or append_value enabled. Skipping.")
        elif exptable[colname][rownum] != cur_default:
            if append_value:
                exp = exptable[rownum]['EXPID']
                raise ValueError(
                    f"In exposure: {exp}. Cannot append to non-empty cell with type: {cur_dtype}. Skipping.")
            elif overwrite_value:
                exptable[rownum] = document_in_comments(exptable[rownum],colname,value)
                exptable[colname][rownum] = np.append(cur_default, value)
            else:
                exp = exptable[rownum]['EXPID']
                raise ValueError (f"In exposure: {exp}. Asked to overwrite non-empty cell of type {cur_dtype} without overwrite_value enabled. Skipping.")
        else:
            exptable[colname][rownum] = value
            exptable[rownum] = document_in_comments(exptable[rownum],colname,value)
    return exptable

def document_in_comments(tablerow,colname,value,comment_col='HEADERERR'):
    existing_entries = [colname in term for term in tablerow[comment_col]]
    if np.any(existing_entries):
        loc = np.where(existing_entries)[0][0]
        entry = tablerow[comment_col][loc]
        key, origval, oldval = deconstruct_document_in_comments(entry)
        if key != colname:
            print("Key didn't match colname in document_in_comments")
            raise
        new_entry = keyval_change_reporting(colname, origval, value)
        tablerow[comment_col][loc] = new_entry
    else:
        reporting = keyval_change_reporting(colname, tablerow[colname], value)
        tablerow[comment_col] = np.append(tablerow[comment_col], reporting)
    return tablerow

def deconstruct_document_in_comments(entry):
    ## Ensure that the rudimentary characteristics are there
    if ':' not in entry or '->' not in entry:
        raise ValueError("Entry must be of the form {key}:{oldval}->{newval}. Exiting")
    ## Get the key left of colon
    entries = entry.split(':')
    key = entries[0]
    ## The values could potentially have colon's. This allows that
    values = ':'.join(entries[1:])
    ## Two values should be separated by text arrow
    val1,val2 = values.split("->")
    return key, val1, val2

def edit_exposure_table(night, exp_str, colname, value, append_value=True, overwrite_value=False,
                        read_user_version=False, write_user_version=False, overwrite_file=True):#, joinsymb='|'):
    ## Don't edit fixed columns
    colname = colname.upper()
    if colname in ['EXPID','CAMWORD']:
        raise ValueError(f"Not allowed to edit colname={colname}.")

    ## Get the file locations
    path = get_exposure_table_path(night=night)
    name = get_exposure_table_name(night=night)#, extension='.csv')
    pathname = pathjoin(path, name)
    if read_user_version or write_user_version:
        user_pathname = os.path.join(path, name.replace('.csv', str(os.environ['USER']) + '.csv'))
    else:
        user_pathname = None

    ## Read in the table
    if read_user_version and os.path.isfile(user_pathname):
        exptable = load_table(tablename=user_pathname, tabletype='exptable')
    else:
        exptable = load_table(tablename=pathname, tabletype='exptable')

    if exptable is None:
        print("There was a problem loading the exposure table... Exiting.")
        return

    ## Do the modification
    outtable = change_exposure_table_rows(exptable, exp_str, colname, value, append_value, overwrite_value)#, joinsymb)

    ## Write out the table
    if write_user_version:
        write_table(outtable, tablename=user_pathname, tabletype='exptable', overwrite=overwrite_file)
    else:
        write_table(outtable, tablename=pathname, tabletype='exptable', overwrite=overwrite_file)