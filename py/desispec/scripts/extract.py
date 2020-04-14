"""
Extract spectra from DESI pre-processed raw data
"""

from __future__ import absolute_import, division, print_function

import sys
import traceback
import os
import re
import os.path
import time
import argparse
import numpy as np
from astropy.io import fits

import specter
from specter.psf import load_psf
from specter.extract import ex2d

from desispec import io
from desiutil.log import get_logger
from desispec.frame import Frame
from desispec.maskbits import specmask

import desispec.scripts.mergebundles as mergebundles
from desispec.specscore import compute_and_append_frame_scores
from desispec.heliocentric import barycentric_velocity_multiplicative_corr

def parse(options=None):
    parser = argparse.ArgumentParser(description="Extract spectra from pre-processed raw data.")
    parser.add_argument("-i", "--input", type=str, required=True,
                        help="input image")
    parser.add_argument("-f", "--fibermap", type=str, required=False,
                        help="input fibermap file")
    parser.add_argument("-p", "--psf", type=str, required=True,
                        help="input psf file")
    parser.add_argument("-o", "--output", type=str, required=True,
                        help="output extracted spectra file")
    parser.add_argument("-m", "--model", type=str, required=False,
                        help="output 2D pixel model file")
    parser.add_argument("-w", "--wavelength", type=str, required=False,
                        help="wavemin,wavemax,dw")
    parser.add_argument("-s", "--specmin", type=int, required=False, default=0,
                        help="first spectrum to extract")
    parser.add_argument("-n", "--nspec", type=int, required=False,
                        help="number of spectra to extract")
    parser.add_argument("-r", "--regularize", type=float, required=False, default=0.0,
                        help="regularization amount (default %(default)s)")
    parser.add_argument("--bundlesize", type=int, required=False, default=25,
                        help="number of spectra per bundle")
    parser.add_argument("--nsubbundles", type=int, required=False, default=6,
                        help="number of extraction sub-bundles")
    parser.add_argument("--nwavestep", type=int, required=False, default=50,
                        help="number of wavelength steps per divide-and-conquer extraction step")
    parser.add_argument("-v", "--verbose", action="store_true", help="print more stuff")
    parser.add_argument("--mpi", action="store_true", help="Use MPI for parallelism")
    parser.add_argument("--decorrelate-fibers", action="store_true", help="Not recommended")
    parser.add_argument("--no-scores", action="store_true", help="Do not compute scores")
    parser.add_argument("--psferr", type=float, default=None, required=False,
                        help="fractional PSF model error used to compute chi2 and mask pixels (default = value saved in psf file)")
    # parser.add_argument("--fibermap-index", type=int, default=None, required=False,
    #                     help="start at this index in the fibermap table instead of using the spectro id from the camera")
    parser.add_argument("--barycentric-correction", action="store_true", help="apply barycentric correction to wavelength")
    
    args = None
    if options is None:
        args = parser.parse_args()
    else:
        args = parser.parse_args(options)
    return args


#- Util function to trim path to something that fits in a fits file (!)
def _trim(filepath, maxchar=40):
    if len(filepath) > maxchar:
        return '...{}'.format(filepath[-maxchar:])

def barycentric_correction_multiplicative_factor(header) :
    """
    Returns mult. barycentric correction factor using coords in `header`

    `header` must contrain MJD or MJD-OBS; and
    TARGTRA,TARGTDEC or SKYRA,SKYDEC or TELRA,TELDEC or RA,DEC
    """

    if "TARGTRA" in header :
        ra  = header["TARGTRA"]
    elif "SKYRA" in header :
        ra = header["SKYRA"]
    elif "TELRA" in header :
        ra = header["TELRA"]
    elif "RA" in header :
        ra  = header["RA"]
    else :
        raise KeyError("no TARGTRA nor RA in header")

    if "TARGTDEC" in header :
        dec = header["TARGTDEC"]
    elif "SKYDEC" in header :
        dec = header["SKYDEC"]
    elif "TELDEC" in header :
        dec = header["TELDEC"]
    elif "DEC" in header :
        dec = header["DEC"]
    else :
        raise KeyError("no TARGTDEC nor DEC in header")

    if "MJD-OBS" in header :
        mjd = header["MJD-OBS"]
    elif "MJD" in header :
        mjd = header["MJD"]
    else :
        raise KeyError("no MJD-OBS nor MJD in header")

    val = barycentric_velocity_multiplicative_corr(ra, dec, mjd)

    log = get_logger()
    log.debug("Barycentric correction factor = {}".format(val))

    return val
    

def main(args):

    if args.mpi:
        from mpi4py import MPI
        comm = MPI.COMM_WORLD
        return main_mpi(args, comm)
    else:
        return main_mpi(args, comm=None)
    

def main_mpi(args, comm=None, timing=None):
    nproc = 1
    rank = 0
    if comm is not None:
        nproc = comm.size
        rank = comm.rank
        
    mark_start = time.time()

    log = get_logger()

    psf_file = args.psf
    input_file = args.input

    # these parameters are interpreted as the *global* spec range,
    # to be divided among processes.
    specmin = args.specmin
    nspec = args.nspec
        
    #- Load input files and broadcast

    # FIXME: after we have fixed the serialization
    # of the PSF, read and broadcast here, to reduce
    # disk contention.

    img = None
    if rank == 0:
        img = io.read_image(input_file)
    if comm is not None:
        img = comm.bcast(img, root=0)
        
    psf = load_psf(psf_file)

    mark_read_input = time.time()

    # get spectral range
    if nspec is None:
        nspec = psf.nspec
        
    if args.fibermap is not None:
        fibermap = io.read_fibermap(args.fibermap)
    else:
        try:
            fibermap = io.read_fibermap(args.input)
        except (AttributeError, IOError, KeyError):
            fibermap = None

    if fibermap is not None:
        fibermap = fibermap[specmin:specmin+nspec]
        if nspec > len(fibermap):
            log.warning("nspec {} > len(fibermap) {}; reducing nspec to {}".format(
                nspec, len(fibermap), len(fibermap)))
            nspec = len(fibermap)
        fibers = fibermap['FIBER']
    else:
        fibers = np.arange(specmin, specmin+nspec)

    specmax = specmin + nspec

    #- Get wavelength grid from options
    if args.wavelength is not None:
        raw_wstart, raw_wstop, raw_dw = [float(tmp) for tmp in args.wavelength.split(',')]
    else:
        raw_wstart = np.ceil(psf.wmin_all)
        raw_wstop = np.floor(psf.wmax_all)
        raw_dw = 0.7

    raw_wave = np.arange(raw_wstart, raw_wstop+raw_dw/2.0, raw_dw)
    nwave = len(raw_wave)
    bundlesize = args.bundlesize
    
    if args.barycentric_correction :
        if ('RA' in img.meta) or ('TARGTRA' in img.meta):
            barycentric_correction_factor = \
                    barycentric_correction_multiplicative_factor(img.meta)
        #- Early commissioning has RA/TARGTRA in fibermap but not HDU 0
        elif fibermap is not None and \
                (('RA' in fibermap.meta) or ('TARGTRA' in fibermap.meta)):
            barycentric_correction_factor = \
                    barycentric_correction_multiplicative_factor(fibermap.meta)
        else:
            msg = 'Barycentric corr requires (TARGT)RA in HDU 0 or fibermap'
            log.critical(msg)
            raise KeyError(msg)
    else :
        barycentric_correction_factor = 1.
    
    # Explictly define the correct wavelength values to avoid confusion of reference frame
    # If correction applied, otherwise divide by 1 and use the same raw values
    wstart = raw_wstart/barycentric_correction_factor
    wstop  = raw_wstop/barycentric_correction_factor
    dw     = raw_dw/barycentric_correction_factor
    wave   = raw_wave/barycentric_correction_factor

    #- Confirm that this PSF covers these wavelengths for these spectra
    psf_wavemin = np.max(psf.wavelength(list(range(specmin, specmax)), y=-0.5))
    psf_wavemax = np.min(psf.wavelength(list(range(specmin, specmax)), y=psf.npix_y-0.5))
    if psf_wavemin-5 > wstart:
        raise ValueError('Start wavelength {:.2f} < min wavelength {:.2f} for these fibers'.format(wstart, psf_wavemin))
    if psf_wavemax+5 < wstop:
        raise ValueError('Stop wavelength {:.2f} > max wavelength {:.2f} for these fibers'.format(wstop, psf_wavemax))
    
    if rank == 0:
        #- Print parameters                                                                                    
        log.info("extract:  input = {}".format(input_file))
        log.info("extract:  psf = {}".format(psf_file))
        log.info("extract:  specmin = {}".format(specmin))
        log.info("extract:  nspec = {}".format(nspec))
        log.info("extract:  wavelength = {},{},{}".format(wstart, wstop, dw))
        log.info("extract:  nwavestep = {}".format(args.nwavestep))
        log.info("extract:  regularize = {}".format(args.regularize))
    
    if barycentric_correction_factor != 1. :
        img.meta['HELIOCOR']   = barycentric_correction_factor

    #- Augment input image header for output                                
    img.meta['NSPEC']   = (nspec, 'Number of spectra')
    img.meta['WAVEMIN'] = (raw_wstart, 'First wavelength [Angstroms]')
    img.meta['WAVEMAX'] = (raw_wstop, 'Last wavelength [Angstroms]')
    img.meta['WAVESTEP']= (raw_dw, 'Wavelength step size [Angstroms]')
    img.meta['SPECTER'] = (specter.__version__, 'https://github.com/desihub/specter')
    img.meta['IN_PSF']  = (_trim(psf_file), 'Input spectral PSF')
    img.meta['IN_IMG']  = (_trim(input_file), 'Input image')

    #- If not using MPI, use a single call to each of these and then end this function call
    #  Otherwise, continue on to splitting things up for the different ranks
    if comm is None:
        _extract_and_save(img, psf, specmin, nspec, specmin,
                          wave, raw_wave, fibers, fibermap,
                          args.output, args.model,
                          bundlesize, args, log)
        
        #- This is it if we aren't running MPI, so return                      
        return
    #else:
    #    # Continue to the MPI section, which could go under this else statment             
    #    # But to save on indentation we'll just pass on to the rest of the function
    #    # since the alternative has already returned
    #    pass

    # Now we divide our spectra into bundles
    checkbundles = set()
    checkbundles.update(np.floor_divide(np.arange(specmin, specmax), bundlesize*np.ones(nspec)).astype(int))
    bundles = sorted(checkbundles)
    nbundle = len(bundles)

    bspecmin = {}
    bnspec = {}
    
    for b in bundles:
        if specmin > b * bundlesize:
            bspecmin[b] = specmin
        else:
            bspecmin[b] = b * bundlesize
        if (b+1) * bundlesize > specmax:
            bnspec[b] = specmax - bspecmin[b]
        else:
            bnspec[b] = bundlesize

    # Now we assign bundles to processes
    mynbundle = int(nbundle // nproc)
    myfirstbundle = 0
    leftover = nbundle % nproc
    if rank < leftover:
        mynbundle += 1
        myfirstbundle = rank * mynbundle
    else:
        myfirstbundle = ((mynbundle + 1) * leftover) + (mynbundle * (rank - leftover))

    # get the root output file
    outpat = re.compile(r'(.*)\.fits')
    outmat = outpat.match(args.output)
    if outmat is None:
        raise RuntimeError("extraction output file should have .fits extension")
    outroot = outmat.group(1)

    outdir = os.path.normpath(os.path.dirname(outroot))

    if rank == 0:
        if not os.path.isdir(outdir):
            os.makedirs(outdir)

    if comm is not None:
        comm.barrier()

    mark_preparation = time.time()
    time_total_extraction = 0.0
    time_total_write_output = 0.0
    failcount = 0
    
    for b in range(myfirstbundle, myfirstbundle+mynbundle):
        mark_iteration_start = time.time()
        outbundle = "{}_{:02d}.fits".format(outroot, b)
        outmodel = "{}_model_{:02d}.fits".format(outroot, b)

        log.info('extract:  Rank {} extracting {} spectra {}:{} at {}'.format(
            rank, os.path.basename(input_file),
            bspecmin[b], bspecmin[b]+bnspec[b], time.asctime(),
            ) )
        sys.stdout.flush()

        #- The actual extraction
        try:
            mark_extraction = _extract_and_save(img, psf, bspecmin[b], bnspec[b], specmin,
                              wave, raw_wave, fibers, fibermap,
                              outbundle, outmodel, bundlesize, args, log)

            mark_write_output = time.time()

            time_total_extraction += mark_extraction - mark_iteration_start
            time_total_write_output += mark_write_output - mark_extraction
        except:
            # Log the error and increment the number of failures
            log.error("extract:  FAILED bundle {}, spectrum range {}:{}".format(b, bspecmin[b], bspecmin[b]+bnspec[b]))
            exc_type, exc_value, exc_traceback = sys.exc_info()
            lines = traceback.format_exception(exc_type, exc_value, exc_traceback)
            log.error(''.join(lines))
            failcount += 1
            sys.stdout.flush()

    if comm is not None:
        failcount = comm.allreduce(failcount)

    if failcount > 0:
        # all processes throw
        raise RuntimeError("some extraction bundles failed")

    time_merge = None
    if rank == 0:
        mark_merge_start = time.time()
        mergeopts = [
            '--output', args.output,
            '--force',
            '--delete'
        ]
        mergeopts.extend([ "{}_{:02d}.fits".format(outroot, b) for b in bundles ])
        mergeargs = mergebundles.parse(mergeopts)
        mergebundles.main(mergeargs)

        if args.model is not None:
            model = None
            for b in bundles:
                outmodel = "{}_model_{:02d}.fits".format(outroot, b)
                if model is None:
                    model = fits.getdata(outmodel)
                else:
                    #- TODO: test and warn if models overlap for pixels with
                    #- non-zero values
                    model += fits.getdata(outmodel)

                os.remove(outmodel)

            fits.writeto(args.model, model)
        mark_merge_end = time.time()
        time_merge = mark_merge_end - mark_merge_start

    # Resolve difference timer data

    if type(timing) is dict:
        timing["read_input"] = mark_read_input - mark_start
        timing["preparation"] = mark_preparation - mark_read_input
        timing["total_extraction"] = time_total_extraction
        timing["total_write_output"] = time_total_write_output
        timing["merge"] = time_merge


def _extract_and_save(img, psf, bspecmin, bnspec, specmin, wave, raw_wave, fibers, fibermap,
                      outbundle, outmodel, bundlesize, args, log):
    '''
    Performs the main extraction and saving of extracted frames found in the body of the 
    main loop. Refactored to be callable by both MPI and non-MPI versions of the code. 
    This should be viewed as a shorthand for the following commands.
    '''
    results = ex2d(img.pix, img.ivar*(img.mask==0), psf, bspecmin,
                   bnspec, wave, regularize=args.regularize, ndecorr=args.decorrelate_fibers,
                   bundlesize=bundlesize, wavesize=args.nwavestep, verbose=args.verbose,
                   full_output=True, nsubbundles=args.nsubbundles)

    flux = results['flux']
    ivar = results['ivar']
    Rdata = results['resolution_data']
    chi2pix = results['chi2pix']

    mask = np.zeros(flux.shape, dtype=np.uint32)
    mask[results['pixmask_fraction']>0.5] |= specmask.SOMEBADPIX
    mask[results['pixmask_fraction']==1.0] |= specmask.ALLBADPIX
    mask[chi2pix>100.0] |= specmask.BAD2DFIT
    
    if fibermap is not None:
        bfibermap = fibermap[bspecmin-specmin:bspecmin+bnspec-specmin]
    else:
        bfibermap = None

    bfibers = fibers[bspecmin-specmin:bspecmin+bnspec-specmin]
    
    #- Save the raw wavelength, not the corrected one (if corrected)                  
    frame = Frame(raw_wave, flux, ivar, mask=mask, resolution_data=Rdata,
                  fibers=bfibers, meta=img.meta, fibermap=bfibermap,
                  chi2pix=chi2pix)

    #- Add unit                                                                           
    #   In specter.extract.ex2d one has flux /= dwave                                     
    #   to convert the measured total number of electrons per                            
    #   wavelength node to an electron 'density'                                             
    frame.meta['BUNIT'] = 'count/Angstrom'
    
    #- Add scores to frame                                                               
    if not args.no_scores :
        compute_and_append_frame_scores(frame,suffix="RAW")

    mark_extraction = time.time()
        
    #- Write output                                                          
    io.write_frame(outbundle, frame)
    
    if args.model is not None:
        fits.writeto(outmodel, results['modelimage'], header=frame.meta)

    log.info('extract:  Done {} spectra {}:{} at {}'.format(os.path.basename(args.input),
                                                            bspecmin, bspecmin+bnspec, time.asctime()))
    sys.stdout.flush()

    return mark_extraction
