'''
Generate Master TSNR ensemble DFLUX files.  See doc. 4723.  Note: in this
instance, ensemble avg. of flux is written, in order to efficiently generate
tile depths.

Currently assumes redshift and mag ranges derived from FDR, but uniform in both.
'''
import os
import sys
import copy
import yaml
import pickle
import desiutil
import fitsio
import desisim
import argparse
import os.path                       as     path
import numpy                         as     np
import astropy.io.fits               as     fits
import matplotlib.pyplot             as     plt

from   desiutil                      import depend
from   astropy.convolution           import convolve, Box1DKernel
from   pathlib                       import Path
from   desiutil.dust                 import mwdust_transmission
from   desiutil.log                  import get_logger
from   pkg_resources                 import resource_filename
from   scipy.interpolate             import interp1d
from   astropy.table                 import Table, join

np.random.seed(seed=314)

# AR/DK DESI spectra wavelengths
# TODO:  where are brz extraction wavelengths defined?  https://github.com/desihub/desispec/issues/1006.                                                                                                                              
wmin, wmax, wdelta = 3600, 9824, 0.8
wave               = np.round(np.arange(wmin, wmax + wdelta, wdelta), 1)
cslice             = {"b": slice(0, 2751), "r": slice(2700, 5026), "z": slice(4900, 7781)}

def parse(options=None):
    parser = argparse.ArgumentParser(description="Generate a sim. template ensemble stack of given type and write it to disk at --outdir.")
    parser.add_argument('--nmodel', type = int, default = 2000, required=False,
                        help='Number of galaxies in the ensemble.')
    parser.add_argument('--tracer', type = str, default = 'bgs', required=True,
                        help='Tracer to generate of [bgs, lrg, elg, qso].')
    parser.add_argument('--configdir', type = str, default = None, required=False,
                        help='Directory to config files if not desispec repo.')
    parser.add_argument('--smooth', type=float, default=100., required=False,
                        help='Smoothing scale [A] for DFLUX calc.')
    parser.add_argument('--Nz', action='store_true',
                        help = 'Apply tracer Nz weighting in stacking of ensemble.')
    parser.add_argument('--external_calib', type=str, required=False, default=None,
                        help='Calibration file, e.g. sv1-exposures.fits, with e.g. EFFTIME_DARK to normalize against.')
    parser.add_argument('--tsnr_run', type=str, required=False,
                        help='TSNR afterburner file, with TSNR2_TRACER.')
    parser.add_argument('--outdir', type = str, default = 'bgs', required=True,
			help='Directory to write to.')
    args = None

    if options is None:
        args = parser.parse_args()
    else:
        args = parser.parse_args(options)

    return args

def tsnr_efftime(external_calib, tsnr_run, tracer, plot=True):
    '''
    Given an external calibration, e.g. 
    /global/cfs/cdirs/desi/survey/observations/SV1/sv1-exposures.fits

    with e.g. EFFTIME_DARK and

    a tsnr afterburner run, e.g. 
    /global/cfs/cdirs/desi/spectro/redux/cascades/tsnr-cascades.fits

    Compute linear coefficient to convert TSNR2_TRACER_BRZ to EFFTIME_DARK
    or EFFTIME_BRIGHT. 
    '''

    tsnr_col  = 'TSNR2_{}'.format(tracer.upper())
    
    ext_calib = Table.read(external_calib)

    # Quality cuts. 
    ext_calib = ext_calib[(ext_calib['EXPTIME'] > 60.)]

    if tracer in ['bgs', 'mws']:
        ext_col   = 'EFFTIME_BRIGHT'

        # Expected BGS exposure is 180s nominal. 
        ext_calib = ext_calib[(ext_calib['EFFTIME_BRIGHT'] > 120.)]

    else:
        ext_col   = 'EFFTIME_DARK'

        # Expected BGS exposure is 900s nominal.   
        ext_calib = ext_calib[(ext_calib['EFFTIME_DARK'] > 450.)]

    tsnr_run  = Table.read(tsnr_run)

    # TSNR == 0.0 if exposure was not successfully reduced. 
    tsnr_run  = tsnr_run[tsnr_run[tsnr_col] > 0.0]

    # Keep common exposures.
    ext_calib = ext_calib[np.isin(ext_calib['EXPID'], tsnr_run['EXPID'])]
    tsnr_run  = tsnr_run[np.isin(tsnr_run['EXPID'], ext_calib['EXPID'])] 
    
    tsnr_run  = join(tsnr_run, ext_calib['EXPID', ext_col], join_type='left', keys='EXPID')
    tsnr_run.sort(ext_col)
    
    tsnr_run.pprint()

    # from   scipy  import stats
    # res       = stats.linregress(tsnr_run[ext_col], tsnr_run[tsnr_col])
    # slope     = res.slope
    # intercept = res.intercept

    slope     = np.sum(tsnr_run[ext_col] * tsnr_run[tsnr_col]) / np.sum(tsnr_run[tsnr_col]**2.)

    if plot:
        plt.plot(tsnr_run[ext_col], tsnr_run[tsnr_col], c='k', marker='.', lw=0.0, markersize=1)
        plt.plot(tsnr_run[ext_col], intercept + slope*tsnr_run[ext_col], c='k', lw=0.5)
        plt.title('{} = {:.3f} x {} + {:.3f}'.format(tsnr_col, slope, ext_col, intercept))
        plt.xlabel(ext_col)
        plt.ylabel(tsnr_col)
        plt.show()

    return  slope
    
class Config(object):
    def __init__(self, cpath):
        with open(cpath) as f:
            d = yaml.load(f, Loader=yaml.FullLoader)
        
        for key in d:
            setattr(self, key, d[key])
        
class template_ensemble(object):
    '''
    Generate an ensemble of templates to sample tSNR for a range of points in
    (z, m, OII, etc.) space.

    If conditioned, uses deepfield redshifts and (currently r) magnitudes
    to condition simulated templates.
    '''
    
    def __init__(self, outdir, tracer='elg', nmodel=5, log=None, configdir=None,
                 Nz=False, smooth=100., calibrate=False):
        """
        Generate a template ensemble for template S/N measurements (tSNR)

        Args:
            outdir: output directory

        Options:
            tracer: 'bgs', 'lrg', 'elg', or 'qso'
            nmodel: number of template models to generate
            log: logger to use
            configdir: directory to override tsnr-config-{tracer}.yaml files
            Nz (bool): if True, apply FDR N(z) weights
            smooth: smoothing scale for <F - smooth(F)>

        Writes {outdir}/tsnr-ensemle-{tracer}.fits files
        """
        if log is None:
            log = get_logger()

        if configdir == None:
            cpath = resource_filename('desispec', 'data/tsnr/tsnr-config-{}.yaml'.format(tracer))
        else:
            cpath = args.configdir + '/tsnr-config-{}.yaml'.format(tracer)

        config = Config(cpath)

        if calibrate:
            log.info('Reading pre-written {} for normalization.'.format('{}/tsnr-ensemble-{}.fits'.format(outdir, tracer)))

            ensemble = fitsio.read('{}/tsnr-ensemble-{}.fits'.format(outdir, tracer))

            calibrate(ensemble)
            
            return 
            
        def tracer_maker(wave, tracer=tracer, nmodel=nmodel, redshifts=None,
                         mags=None, config=None):
            '''
            Dedicated wrapper for desisim.templates.GALAXY.make_templates call,
            stipulating templates in a redshift range suggested by the FDR.
            Further, assume fluxes close to the expected (within ~0.5 mags.)
            in the appropriate band.   

            Class init will write ensemble stack to disk at outdir, for a given
            tracer [bgs, lrg, elg, qso], having generated nmodel templates.
            Optionally, provide redshifts and mags. to condition appropriately
            at cost of runtime.
            '''
            # Only import desisim if code is run, not at module import
            # to minimize desispec -> desisim -> desispec dependency loop
            import desisim.templates

            tracer = tracer.lower()  # support ELG or elg, etc.

            # https://arxiv.org/pdf/1611.00036.pdf
            #
            normfilter_south=config.filter

            zrange   = (config.zlo, config.zhi)
 
            # Variance normalized as for psf, so we need an additional linear
            # flux loss so account for the relative factors.
            psf_loss = -config.psf_fiberloss / 2.5
            psf_loss = 10.**psf_loss
            
            rel_loss = -(config.wgt_fiberloss - config.psf_fiberloss) / 2.5
            rel_loss = 10.**rel_loss

            log.info('{} nmodel: {:d}'.format(tracer, nmodel))            
            log.info('{} filter: {}'.format(tracer, config.filter))
            log.info('{} zrange: {} - {}'.format(tracer, zrange[0], zrange[1]))

            # Calibration vector assumes PSF mtype.
            log.info('psf fiberloss: {:.3f}'.format(psf_loss))
            log.info('Relative fiberloss to psf morphtype: {:.3f}'.format(rel_loss))

            if tracer == 'bgs':
                # Cut on mag. 
                # https://github.com/desihub/desitarget/blob/dd353c6c8dd8b8737e45771ab903ac30584db6db/py/desitarget/cuts.py#L1312
                magrange = (config.med_mag, config.limit_mag)
                
                maker = desisim.templates.BGS(wave=wave, normfilter_south=normfilter_south)
                flux, wave, meta, objmeta = maker.make_templates(nmodel=nmodel, redshift=redshifts, mag=mags, south=True, zrange=zrange, magrange=magrange)

                # Additional factor rel. to psf.; TSNR put onto instrumental
                # e/A given calibration vector that includes psf-like loss.
                flux *= rel_loss
                
            elif tracer == 'lrg':
                # Cut on fib. mag. with desisim.templates setting FIBERFLUX to FLUX. 
                # https://github.com/desihub/desitarget/blob/dd353c6c8dd8b8737e45771ab903ac30584db6db/py/desitarget/cuts.py#L447
                magrange = (config.med_fibmag, config.limit_fibmag)
                
                maker = desisim.templates.LRG(wave=wave, normfilter_south=normfilter_south)
                flux, wave, meta, objmeta = maker.make_templates(nmodel=nmodel, redshift=redshifts, mag=mags, south=True, zrange=zrange, magrange=magrange)

                # Take factor rel. to psf.; TSNR put onto instrumental
                # e/A given calibration vector that includes psf-like loss.
                # Note:  Oppostive to other tracers as templates normalized to fibermag.  
                flux /=	psf_loss
                
            elif tracer == 'elg':
                # Cut on mag. 
                # https://github.com/desihub/desitarget/blob/dd353c6c8dd8b8737e45771ab903ac30584db6db/py/desitarget/cuts.py#L517
                magrange = (config.med_mag, config.limit_mag)
                
                maker = desisim.templates.ELG(wave=wave, normfilter_south=normfilter_south)
                flux, wave, meta, objmeta = maker.make_templates(nmodel=nmodel, redshift=redshifts, mag=mags, south=True, zrange=zrange, magrange=magrange)

                # Additional factor rel. to psf.; TSNR put onto instrumental
                # e/A given calibration vector that includes psf-like loss.
                flux *=	rel_loss
                
            elif tracer == 'qso':
                # Cut on mag. 
                # https://github.com/desihub/desitarget/blob/dd353c6c8dd8b8737e45771ab903ac30584db6db/py/desitarget/cuts.py#L1422
                magrange = (config.med_mag, config.limit_mag)
                
                maker = desisim.templates.QSO(wave=wave, normfilter_south=normfilter_south)
                flux, wave, meta, objmeta = maker.make_templates(nmodel=nmodel, redshift=redshifts, mag=mags, south=True, zrange=zrange, magrange=magrange)

                # Additional factor rel. to psf.; TSNR put onto instrumental
                # e/A given calibration vector that includes psf-like loss.
                flux *=	rel_loss
                
            else:
                raise  ValueError('{} is not an available tracer.'.format(tracer))

            log.info('{} magrange: {} - {}'.format(tracer, magrange[0], magrange[1]))
            
            return  wave, flux, meta, objmeta


        ## 
        _, flux, meta, objmeta         = tracer_maker(wave, tracer=tracer, nmodel=nmodel, config=config)
                
        self.ensemble_flux             = {}
        self.ensemble_dflux            = {}
        self.ensemble_meta             = meta
        self.ensemble_objmeta          = objmeta
        self.ensemble_dflux_stack      = {}

        ##
        smoothing = np.ceil(smooth / wdelta).astype(np.int)

        log.info('Applying {:.3f} AA smoothing ({:d} pixels)'.format(smooth, smoothing))
 
        # Generate template (d)fluxes for brz bands.
        for band in ['b', 'r', 'z']:
            band_wave                 = wave[cslice[band]]
            in_band                   = np.isin(wave, band_wave)
            self.ensemble_flux[band]  = flux[:, in_band]
            dflux                     = np.zeros_like(self.ensemble_flux[band])

            # Retain only spectral features < 100. Angstroms.
            # dlambda per pixel = 0.8; 100A / dlambda per pixel = 125.
            for i, ff in enumerate(self.ensemble_flux[band]):
                sflux                 = convolve(ff, Box1DKernel(smoothing), boundary='extend')
                dflux[i,:]            = ff - sflux

            self.ensemble_dflux[band] = dflux

        zs = meta['REDSHIFT'].data
            
        if Nz:
            log.info('Applying FDR N(Z) weights.')
            
            # Get tracer N(z) [Total number per sq deg per dz=0.1 redshift bin].
            zmin, zmax, N = np.loadtxt(os.environ['DESIMODEL'] + '/data/targets/nz_{}.dat'.format(tracer), unpack=True, usecols = (0,1,2))
            zmid = 0.5 * (zmin + zmax)

            interp  = interp1d(zmid, N, kind='linear', copy=True, bounds_error=True, fill_value=None, assume_sorted=False)
            weights = interp(zs)
            
        else:
            log.info('Assuming uniform in z stack.')
            
            weights = np.ones_like(zs)
            
        # Stack ensemble.
        for band in ['b', 'r', 'z']:
            self.ensemble_dflux_stack[band] = np.sqrt(np.average(self.ensemble_dflux[band]**2., weights=weights, axis=0).reshape(1, len(self.ensemble_dflux[band].T)))

        hdr = fits.Header()
        hdr['NMODEL']   = nmodel
        hdr['TRACER']   = tracer
        hdr['FILTER']   = config.filter
        hdr['ZLO']      = config.zlo
        hdr['ZHI']      = config.zhi
        hdr['MEDMAG']   = config.med_mag
        hdr['LIMMAG']   = config.limit_mag
        hdr['PSFFLOSS'] = config.psf_fiberloss
        hdr['WGTFLOSS'] = config.wgt_fiberloss
        hdr['SMOOTH']   = smooth
        
        hdu_list = [fits.PrimaryHDU(header=hdr)]

        for band in ['b', 'r', 'z']:
            hdu_list.append(fits.ImageHDU(wave[cslice[band]], name='WAVE_{}'.format(band.upper())))
            hdu_list.append(fits.ImageHDU(self.ensemble_dflux_stack[band], name='DFLUX_{}'.format(band.upper())))

        if Nz:
            hdu_list.append(fits.ImageHDU(np.c_[zs, weights], name='WEIGHTS'))
            
        hdu_list = fits.HDUList(hdu_list)
            
        hdu_list.writeto('{}/tsnr-ensemble-{}.fits'.format(outdir, tracer), overwrite=True)

        log.info('Successfully written to {}.'.format('{}/tsnr-ensemble-{}.fits'.format(outdir, tracer)))
        
def main():
    log = get_logger()

    args = parse()

    # TSNR template generation. 
    if args.external_calib is None:
        rads = template_ensemble(args.outdir, tracer=args.tracer, nmodel=args.nmodel, log=log, configdir=args.configdir, Nz=args.Nz, smooth=args.smooth)

    # Calibration of raw TSNR to EFFTIMEs. 
    else:
        if len(args.external_calib) == 0:
            # If empty string, default to immutable sv1-exposures.fits 
            args.external_calib = resource_filename('desispec', 'data/tsnr/tsnr-efftime-calibration.yaml')

            with open(args.external_calib) as f:
                calib_yaml = yaml.load(f, Loader=yaml.FullLoader)

        keyword="TSNR_CALIB_PATH"

        if not keyword in calib_yaml:
            message="Failed to find {} in {}".format(keyword, args.external_calib)
            raise KeyError(message)

        else:
            args.external_calib = calib_yaml[keyword]
        
        slope = tsnr_efftime(args.external_calib, args.tsnr_run, args.tracer)
        
        log.info('Appending TSNR2TOEFFTIME coefficient of {:.6f} to {}/tsnr-ensemble-{}.fits.'.format(slope, args.outdir, args.tracer))

        ens = fits.open('{}/tsnr-ensemble-{}.fits'.format(args.outdir, args.tracer))  
        hdr = ens[0].header

        hdr['TSNR2TOEFFTIME'] = slope
        hdr['EFFTIMEFILE']    = args.external_calib.replace('/global/cfs/cdirs/desi/survey', '$DESISURVEYOPS')
        hdr['TSNRRUNFILE']    = args.tsnr_run.replace('/global/cfs/cdirs/desi/spectro/redux',        '$REDUX')
                        
        depend.setdep(hdr, 'desisim',  desisim.__version__)
        depend.setdep(hdr, 'desiutil', desiutil.__version__)

        ens.writeto('{}/tsnr-ensemble-{}.fits'.format(args.outdir, args.tracer), overwrite=True)

    log.info('Done.')
        
if __name__ == '__main__':
    main()
