#!/usr/bin/env python
# coding: utf-8


import numpy as np
import os
import sys
import time
from astropy.table import Table
import glob

## Import some helper functions, you can see their definitions by uncomenting the bash shell command
from desispec.workflow.tableio import load_tables, write_tables, write_table
from desispec.workflow.utils import verify_variable_with_environment, pathjoin, listpath, get_printable_banner
from desispec.workflow.timing import during_operating_hours, what_night_is_it, nersc_start_time, nersc_end_time
from desispec.workflow.exptable import default_exptypes_for_exptable, get_exposure_table_column_defs, validate_badamps, \
                                       get_exposure_table_path, get_exposure_table_name, summarize_exposure
from desispec.workflow.proctable import default_exptypes_for_proctable, get_processing_table_path, get_processing_table_name, erow_to_prow
from desispec.workflow.procfuncs import parse_previous_tables, flat_joint_fit, arc_joint_fit, get_type_and_tile, \
                                        science_joint_fit, define_and_assign_dependency, create_and_submit, \
                                        update_and_recurvsively_submit, checkfor_and_submit_joint_job
from desispec.workflow.queue import update_from_queue, any_jobs_not_complete
from desispec.io.util import difference_camwords, parse_badamps

def daily_processing_manager(specprod=None, exp_table_path=None, proc_table_path=None, path_to_data=None,
                             expobstypes=None, procobstypes=None, z_submit_types=None, camword=None, badcamword=None,
                             badamps=None, override_night=None, tab_filetype='csv', queue='realtime',
                             exps_to_ignore=None, data_cadence_time=300, queue_cadence_time=1800,
                             dry_run_level=0, dry_run=False, no_redshifts=False, continue_looping_debug=False, dont_check_job_outputs=False,
                             dont_resubmit_partial_jobs=False, verbose=False):
    """
    Generates processing tables for the nights requested. Requires exposure tables to exist on disk.

    Args:
        specprod: str. The name of the current production. If used, this will overwrite the SPECPROD environment variable.
        exp_table_path: str. Full path to where to exposure tables are stored, WITHOUT the monthly directory included.
        proc_table_path: str. Full path to where to processing tables to be written.
        path_to_data: str. Path to the raw data.
        expobstypes: str or comma separated list of strings. The exposure OBSTYPE's that you want to include in the exposure table.
        procobstypes: str or comma separated list of strings. The exposure OBSTYPE's that you want to include in the processing table.
        z_submit_types: list of str's or comma separated list of string. The "group" types of redshifts that should be
                                       submitted with each exposure. If not specified, default for daily processing is
                                       ['cumulative', 'pernight-v0']. If false, 'false', or [], then no redshifts are submitted.
        camword: str. Camword that, if set, alters the set of cameras that will be set for processing.
                      Examples: a0123456789, a1, a2b3r3, a2b3r4z3.
        badcamword: str. Camword that, if set, will be removed from the camword defined in camword if given, or the camword
                         inferred from the data if camword is not given.
        badamps: str. Comma seperated list of bad amplifiers that should not be processed. Should be of the
                      form "{camera}{petal}{amp}", i.e. "[brz][0-9][ABCD]". Example: 'b7D,z8A'
        override_night: str or int. 8 digit night, e.g. 20200314, of data to run on. If None, it runs on the current night.
        tab_filetype: str. The file extension (without the '.') of the exposure and processing tables.
        queue: str. The name of the queue to submit the jobs to. Default is "realtime".
        exps_to_ignore: list. A list of exposure id's that should not be processed. Each should be an integer.
        data_cadence_time: int. Wait time in seconds between loops in looking for new data. Default is 30 seconds.
        queue_cadence_time: int. Wait time in seconds between loops in checking queue statuses and resubmitting failures. Default is 1800s.
        dry_run_level, int, If nonzero, this is a simulated run. If dry_run=1 the scripts will be written or submitted. If
                      dry_run=2, the scripts will not be writter or submitted. Logging will remain the same
                      for testing as though scripts are being submitted. Default is 0 (false).
        dry_run, bool. When to run without submitting scripts or not. If dry_run_level is defined, then it over-rides
                       this flag. dry_run_level not set and dry_run=True, dry_run_level is set to 2 (no scripts
                       generated or run). Default for dry_run is False.
        no_redshifts, bool. Whether to submit redshifts or not. If True, redshifts are not submitted.
        continue_looping_debug: bool. FOR DEBUG purposes only. Will continue looping in search of new data until the process
                                 is terminated. Default is False.
        dont_check_job_outputs, bool. Default is False. If False, the code checks for the existence of the expected final
                                 data products for the script being submitted. If all files exist and this is False,
                                 then the script will not be submitted. If some files exist and this is False, only the
                                 subset of the cameras without the final data products will be generated and submitted.
        dont_resubmit_partial_jobs, bool. Default is False. Must be used with dont_check_job_outputs=False. If this flag is
                                          False, jobs with some prior data are pruned using PROCCAMWORD to only process the
                                          remaining cameras not found to exist.
        verbose: bool. True if you want more verbose output, false otherwise. Current not propagated to lower code,
                       so it is only used in the main daily_processing script itself.

    Returns: Nothing

    Notes:
        Generates both exposure table and processing tables 'on the fly' and saves them at various checkpoints. These
        should be capable of being reloaded in case of interuption or accidental termination of the manager's process.
    """
    ## If not being done during operating hours, and we're not simulating data or running a catchup run, exit.
    if not during_operating_hours(dry_run=dry_run) and override_night is None:
        print("Not during operating hours, and not asked to perform a dry run or run on historic data. Exiting.")
        sys.exit(0)

    ## What night are we running on?
    true_night = what_night_is_it()
    if override_night is not None:
        night = int(override_night)
        print(f"True night is {true_night}, but running for night={night}")
    else:
        night = true_night

    if continue_looping_debug:
        print("continue_looping_debug is set. Will continue looking for new data and needs to be terminated by the user.")

    ## Recast booleans from double negative
    check_for_outputs = (not dont_check_job_outputs)
    resubmit_partial_complete = (not dont_resubmit_partial_jobs)

    ## Define the obstypes to process
    if procobstypes is None:
        procobstypes = default_exptypes_for_proctable()
    elif isinstance(procobstypes, str):
        procobstypes = procobstypes.split(',')

    ## Define the obstypes to save information for in the exposure table
    if expobstypes is None:
        expobstypes = default_exptypes_for_exptable()
    elif isinstance(expobstypes, str):
        expobstypes = expobstypes.split(',')

    ## Define the group types of redshifts you want to generate for each tile
    if no_redshifts:
        z_submit_types = None
    else:
        if z_submit_types is None:
            pass
        elif isinstance(z_submit_types, str):
            if z_submit_types.lower() == 'false':
                z_submit_types = None
            elif z_submit_types.lower() == 'none':
                z_submit_types = None
            else:
                z_submit_types = [ztype.strip().lower() for ztype in z_submit_types.split(',')]
                for ztype in z_submit_types:
                    if ztype not in ['cumulative', 'pernight-v0', 'pernight', 'perexp']:
                        raise ValueError(f"Couldn't understand ztype={ztype} in z_submit_types={z_submit_types}.")
        else:
            raise ValueError(f"Couldn't understand z_submit_types={z_submit_types}, type={type(z_submit_types)}.")

    if z_submit_types is None:
        print("Not submitting scripts for redshift fitting")
    else:
        print(f"Redshift fitting with redshift group types: {z_submit_types}")

    ## Reconcile the dry_run and dry_run_level
    if dry_run and dry_run_level == 0:
        dry_run_level = 2
    elif dry_run_level > 0:
        dry_run = True

    ## expobstypes must contain all the types used in processing
    for typ in procobstypes:
        if typ not in expobstypes:
            expobstypes.append(typ)

    ## Warn people if changing camword
    finalcamword = 'a0123456789'
    if camword is not None and badcamword is None:
        badcamword = difference_camwords(finalcamword,camword)
        finalcamword = camword
    elif camword is not None and badcamword is not None:
        finalcamword = difference_camwords(camword, badcamword)
        badcamword = difference_camwords('a0123456789', finalcamword)
    elif badcamword is not None:
        finalcamword = difference_camwords(finalcamword,badcamword)
    else:
        badcamword = ''

    if badcamword != '':
        ## Inform the user what will be done with it.
        print(f"Modifying camword of data to be processed with badcamword: {badcamword}. "+\
              f"Camword to be processed: {finalcamword}")

    ## Make sure badamps is formatted properly
    if badamps is None:
        badamps = ''
    else:
        badamps = validate_badamps(badamps)

    ## Define the set of exposures to ignore
    if exps_to_ignore is None:
        exps_to_ignore = set()
    else:
        exps_to_ignore = np.sort(np.array(exps_to_ignore).astype(int))
        print(f"\nReceived exposures to ignore: {exps_to_ignore}")
        exps_to_ignore = set(exps_to_ignore)
        
    ## Adjust wait times if simulating things
    speed_modifier = 1
    if dry_run:
        speed_modifier = 0.1

    ## Get context specific variable values
    nersc_start = nersc_start_time(night=true_night)
    nersc_end = nersc_end_time(night=true_night)
    colnames, coltypes, coldefaults = get_exposure_table_column_defs(return_default_values=True)

    ## Define where to find the data
    path_to_data = verify_variable_with_environment(var=path_to_data,var_name='path_to_data', env_name='DESI_SPECTRO_DATA')
    specprod = verify_variable_with_environment(var=specprod,var_name='specprod',env_name='SPECPROD')

    ## Define the files to look for
    file_glob = os.path.join(path_to_data, str(night), '*', 'checksum-*')

    ## Determine where the exposure table will be written
    if exp_table_path is None:
        exp_table_path = get_exposure_table_path(night=night, usespecprod=True)
    os.makedirs(exp_table_path, exist_ok=True)
    name = get_exposure_table_name(night=night, extension=tab_filetype)
    exp_table_pathname = pathjoin(exp_table_path, name)

    ## Determine where the processing table will be written
    if proc_table_path is None:
        proc_table_path = get_processing_table_path()
    os.makedirs(proc_table_path, exist_ok=True)
    name = get_processing_table_name(prodmod=night, extension=tab_filetype)
    proc_table_pathname = pathjoin(proc_table_path, name)

    ## Determine where the unprocessed data table will be written
    unproc_table_pathname = pathjoin(proc_table_path,name.replace('processing', 'unprocessed'))

    ## Combine the table names and types for easier passing to io functions
    table_pathnames = [exp_table_pathname, proc_table_pathname, unproc_table_pathname]
    table_types = ['exptable','proctable','unproctable']

    ## Load in the files defined above
    etable, ptable, unproc_table = load_tables(tablenames=table_pathnames, \
                                               tabletypes=table_types)

    ## Get relevant data from the tables
    all_exps = set(etable['EXPID'])
    arcs,flats,sciences, arcjob,flatjob, \
    curtype,lasttype, curtile,lasttile, internal_id = parse_previous_tables(etable, ptable, night)

    ## While running on the proper night and during night hours,
    ## or doing a dry_run or override_night, keep looping
    while ( (night == what_night_is_it()) and during_operating_hours(dry_run=dry_run) ) or ( override_night is not None ):
        ## Get a list of new exposures that have been found
        print(f"\n\n\nPreviously known exposures: {all_exps}")
        located_exps = set(sorted([int(os.path.basename(os.path.dirname(fil))) for fil in glob.glob(file_glob)]))
        new_exps = located_exps.difference(all_exps)
        all_exps = located_exps # i.e. new_exps.union(all_exps)
        print(f"\nNew exposures: {new_exps}\n\n")

        ## If there aren't any new exps and there won't be more because we're running on an old night or simulating things, exit
        if (not continue_looping_debug) and ( override_night is not None ) and ( len(list(new_exps))==0 ):
            print("Terminating the search for new exposures because no new exposures are present and you have" + \
                  " override_night set without continue_looping_debug")
            break

        ## Loop over new exposures and process them as relevant to that type
        for exp in sorted(list(new_exps)):
            if verbose:
                print(get_printable_banner(str(exp)))
            else:
                print(f'\n\n##################### {exp} #########################')

            ## Open relevant raw data files to understand what we're dealing with
            erow = summarize_exposure(path_to_data, night, exp, expobstypes, colnames, coldefaults, verbosely=False)

            ## If there was an issue, continue. If it's a string summarizing the end of some sequence, use that info.
            ## If the exposure is assosciated with data, process that data.
            if erow is None:
                continue
            elif type(erow) is str:
                if exp in exps_to_ignore:
                    print(f"Located {erow} in exposure {exp}, but the exposure was listed in the expids to ignore. Ignoring this.")
                elif erow == 'endofarcs' and arcjob is None and 'arc' in procobstypes:
                    print("\nLocated end of arc calibration sequence flag. Processing psfnight.\n")
                    ptable, arcjob, internal_id = arc_joint_fit(ptable, arcs, internal_id, dry_run=dry_run_level, queue=queue)
                elif erow == 'endofflats' and flatjob is None and 'flat' in procobstypes:
                    print("\nLocated end of long flat calibration sequence flag. Processing nightlyflat.\n")
                    ptable, flatjob, internal_id = flat_joint_fit(ptable, flats, internal_id, dry_run=dry_run_level, queue=queue)
                elif 'short' in erow and flatjob is None:
                    print("\nLocated end of short flat calibration flag. Removing flats from list for nightlyflat processing.\n")
                    flats = []
                continue
            else:
                ## Else it's a real row so start processing it
                pass

            erow['BADCAMWORD'] = badcamword
            erow['BADAMPS'] = badamps
            unproc = False
            if exp in exps_to_ignore:
                print("\n{} given as exposure id to ignore. Not processing.".format(exp))
                erow['LASTSTEP'] = 'ignore'
                # erow['EXPFLAG'] = np.append(erow['EXPFLAG'], )
                unproc = True
            elif erow['LASTSTEP'] == 'ignore':
                print("\n{} identified by the pipeline as something to ignore. Not processing.".format(exp))
                unproc = True
            elif erow['OBSTYPE'] not in procobstypes:
                print("\n{} not in obstypes to process: {}. Not processing.".format(erow['OBSTYPE'], procobstypes))
                unproc = True
            elif str(erow['OBSTYPE']).lower() == 'arc' and float(erow['EXPTIME']) > 8.0:
                print("\nArc exposure with EXPTIME greater than 8s. Not processing.")
                unproc = True

            print(f"\nFound: {erow}")
            etable.add_row(erow)
            if unproc:
                unproc_table.add_row(erow)
                continue

            curtype,curtile = get_type_and_tile(erow)

            if lasttype is not None and ((curtype != lasttype) or (curtile != lasttile)):
                ptable, arcjob, flatjob, \
                sciences, internal_id = checkfor_and_submit_joint_job(ptable, arcs, flats, sciences, arcjob, flatjob,
                                                                      lasttype, internal_id, dry_run=dry_run_level,
                                                                      queue=queue, strictly_successful=False,
                                                                      check_for_outputs=check_for_outputs,
                                                                      resubmit_partial_complete=resubmit_partial_complete,
                                                                      z_submit_types=z_submit_types)

            prow = erow_to_prow(erow)
            prow['INTID'] = internal_id
            internal_id += 1
            prow['JOBDESC'] = prow['OBSTYPE']
            prow = define_and_assign_dependency(prow, arcjob, flatjob)
            print(f"\nProcessing: {prow}\n")
            prow = create_and_submit(prow, dry_run=dry_run_level, queue=queue,
                                     strictly_successful=False, check_for_outputs=check_for_outputs,
                                     resubmit_partial_complete=resubmit_partial_complete)
            ptable.add_row(prow)

            ## Note: Assumption here on number of flats
            if curtype == 'flat' and flatjob is None and int(erow['SEQTOT']) < 5:
                flats.append(prow)
            elif curtype == 'arc' and arcjob is None:
                arcs.append(prow)
            elif curtype == 'science' and prow['LASTSTEP'] != 'skysub':
                sciences.append(prow)

            lasttile = curtile
            lasttype = curtype

            ## Flush the outputs
            sys.stdout.flush()
            sys.stderr.flush()

            time.sleep(10*speed_modifier)
            write_tables([etable, ptable, unproc_table],
                         tablenames=[exp_table_pathname, proc_table_pathname, unproc_table_pathname])

        print("\nReached the end of curent iteration of new exposures.")
        print("Waiting {}s before looking for more new data".format(data_cadence_time*speed_modifier))
        time.sleep(data_cadence_time*speed_modifier)

        if len(ptable) > 0:
            ptable = update_from_queue(ptable, start_time=nersc_start, end_time=nersc_end, dry_run=dry_run_level)
            # ptable, nsubmits = update_and_recurvsively_submit(ptable,start_time=nersc_start,end_time=nersc_end,
            #                                                   ptab_name=proc_table_pathname, dry_run=dry_run_level)

            ## Exposure table doesn't change in the interim, so no need to re-write it to disk
            write_table(ptable, tablename=proc_table_pathname)
            time.sleep(30*speed_modifier)

    ## Flush the outputs
    sys.stdout.flush()
    sys.stderr.flush()
    ## No more data coming in, so do bottleneck steps if any apply
    ptable, arcjob, flatjob, \
    sciences, internal_id = checkfor_and_submit_joint_job(ptable, arcs, flats, sciences, arcjob, flatjob,
                                                          lasttype, internal_id, dry_run=dry_run_level,
                                                          queue=queue, strictly_successful=False,
                                                          check_for_outputs=check_for_outputs,
                                                          resubmit_partial_complete=resubmit_partial_complete,
                                                          z_submit_types=z_submit_types)

    ## All jobs now submitted, update information from job queue and save
    ptable = update_from_queue(ptable,start_time=nersc_start,end_time=nersc_end, dry_run=dry_run_level)
    write_table(ptable, tablename=proc_table_pathname)

    print(f"Completed submission of exposures for night {night}.")

    # #######################################
    # ########## Queue Cleanup ##############
    # #######################################
    # print("Now resolving job failures.")
    #
    # ## Flush the outputs
    # sys.stdout.flush()
    # sys.stderr.flush()
    # ## Now we resubmit failed jobs and their dependencies until all jobs have un-submittable end state
    # ## e.g. they either succeeded or failed with a code-related issue
    # ii,nsubmits = 0, 0
    # while ii < 4 and any_jobs_not_complete(ptable['STATUS']):
    #     print(f"Starting iteration {ii} of queue updating and resubmissions of failures.")
    #     ptable, nsubmits = update_and_recurvsively_submit(ptable, submits=nsubmits, start_time=nersc_start,end_time=nersc_end,
    #                                                       ptab_name=proc_table_pathname, dry_run=dry_run_level)
    #     write_table(ptable, tablename=proc_table_pathname)
    #     if any_jobs_not_complete(ptable['STATUS']):
    #         time.sleep(queue_cadence_time*speed_modifier)
    #
    #     ptable = update_from_queue(ptable,start_time=nersc_start,end_time=nersc_end)
    #     write_table(ptable, tablename=proc_table_pathname)
    #     ## Flush the outputs
    #     sys.stdout.flush()
    #     sys.stderr.flush()
    #     ii += 1
    #
    # print("No job failures left.")
    print("Exiting")
    ## Flush the outputs
    sys.stdout.flush()
    sys.stderr.flush()
