# Script for generating QA for a single Frame
from __future__ import absolute_import, division

from desispec.qa import QA_Prod
from desiutil.log import get_logger
import argparse
import numpy as np


def parse(options=None):
    parser = argparse.ArgumentParser(description="Generate Production Level QA")
    parser.add_argument('--specprod_dir', type = str, default = None, required=False,
                        help = 'Path containing the exposures/directory to use.')


    args = None
    if options is None:
        args = parser.parse_args()
    else:
        args = parser.parse_args(options)
    return args


def main(args) :

    log=get_logger()

    log.info("starting")

    qa_prod = QA_Prod(args.specprod_dir)

    # Remake Frame QA?
    if args.make_frameqa > 0:
        log.info("(re)generating QA related to frames")
        if (args.make_frameqa % 4) >= 2:
            make_frame_plots = True
        else:
            make_frame_plots = False
        # Run
        qa_prod.make_frameqa(make_plots=make_frame_plots, clobber=args.clobber)

    # Slurp?
    if args.slurp:
        qa_prod.slurp(make=(args.make_frameqa > 0), remove=args.remove)

    # Channel histograms
    if args.channel_hist is not None:
        # imports
        from matplotlib.backends.backend_pdf import PdfPages
        from desispec.qa import qa_plots as dqqp
        #
        qa_prod.load_data()
        outfile = qa_prod.prod_name+'_chist.pdf'
        pp = PdfPages(outfile)
        # Default?
        if args.channel_hist == 'default':
            dqqp.prod_channel_hist(qa_prod, 'FIBERFLAT', 'MAX_RMS', pp=pp, close=False)
            dqqp.prod_channel_hist(qa_prod, 'SKYSUB', 'MED_RESID', xlim=(-1,1), pp=pp, close=False)
            dqqp.prod_channel_hist(qa_prod, 'FLUXCALIB', 'MAX_ZP_OFF', pp=pp, close=False)
        # Finish
        print("Writing {:s}".format(outfile))
        pp.close()
