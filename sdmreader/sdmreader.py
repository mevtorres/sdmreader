# Copyright 2014 Casey Law <caseyjlaw@gmail.com> and collaborators.
# Licensed under GNU GPL v2.

""" sdmreader -- functions for reading data and metadata from SDM format files

Functions:

read_bdf -- reads data from binary data format and returns numpy array.
calc_uvw -- parses metadata to calculate uvw coordinates for given scan (or time/direction). returns (u,v,w) tuple. Requires CASA libraries.
read_metadata -- parses metadata of SDM file (xml format) to return tuple with two dictionaries (scaninfo, sourceinfo). Scan info defines BDF location per scan.

BDFData class does the heavy lifting to parse binary data format and return numpy array. Does not yet parse flag table.

Note: baseline order used in the bdf is a bit unusual and different from what is assumed when working with a measurement set.
Order of uvw and axis=1 of data array Pythonically would be [i*nants+j for j in range(nants) for i in range(j)], so [ (1,2), (1,3), (2,3), (1,4), ...].
"""

import numpy as np
import os, mmap, math, string, sdmpy, logging
import cPickle as pickle
import xml.etree.ElementTree as et    # sdmpy can do this part...
from email.feedparser import FeedParser
from email.message import Message

logger = logging.getLogger(__name__)

def read_bdf(sdmpath, scan, nskip=0, readints=0, writebdfpkl=False):
    """ Reads given range of integrations from sdm of given scan.
    Uses BDFData object to read.
    readints=0 will read all of bdf (skipping nskip).
    Option to write pkl to store bdf info for faster parse next time.
    """

    assert os.path.exists(sdmpath), 'sdmpath %s does not exist' % sdmpath
    scans, sources = read_metadata(sdmpath, scan)
    assert scans[scan]['bdfstr'], 'bdfstr not defined for scan %d' % scan
    bdffile = scans[scan]['bdfstr']

    assert os.path.exists(bdffile), 'Could not find bdf for scan %d and bdfstr %s.' % (scan, scans[scan]['bdfstr'])

    with open(bdffile, 'r') as fp:
        # define bdfpkldir
        if writebdfpkl:
            bdfpkldir = os.path.join(sdmpath, 'bdfpkls')   # make place for bdfpkls, if needed
            if not os.path.exists(bdfpkldir):
                os.makedirs(bdfpkldir)
        else:
            bdfpkldir = ''

        bdf = BDFData(fp, bdfpkldir=bdfpkldir).parse()
        if readints == 0:
            readints = bdf.n_integrations - nskip

        logger.info('Reading %d ints starting at int %d' % (readints, nskip))
        data = np.empty( (readints, bdf.n_baselines, bdf.n_channels, len(bdf.crosspols)), dtype='complex64', order='C')
        for i in xrange(readints):
            data[i] = bdf.get_data ('crossData.bin', i+nskip)
#            flag[i] = bdf.get_data ('flags.bin', i+nskip)  # need to get auto+cross parsing right to implement this

    return data

def calc_uvw(sdmfile, scan=0, datetime=0, radec=()):
    """ Calculates and returns uvw in meters for a given SDM, time, and pointing direction.
    sdmfile is path to sdm directory that includes "Station.xml" file.
    scan is scan number defined by observatory.
    datetime is time (as string) to calculate uvw (format: '2014/09/03/08:33:04.20')
    radec is (ra,dec) as tuple in units of degrees (format: (180., +45.))
    """

    # set up CASA tools
    try:
        # try casapy-free casa
        import casautil
        me = casautil.tools.measures()
        qa = casautil.tools.quanta()
        logger.debug('Accessing CASA libraries with casautil.')
    except ImportError:
        try:
            from casa import quanta as qa
            from casa import measures as me
            logger.debug('Accessing CASA libraries with casapy.')
        except ImportError:
            logger.warning('No CASA libraries available. Cannot run calc_uvw.')
            exit(1)

    assert os.path.exists(os.path.join(sdmfile, 'Station.xml')), 'sdmfile %s has no Station.xml file. Not an SDM?' % sdmfile

    # get scan info
    scans, sources = read_metadata(sdmfile, scan)

    # default is to use scan info
    if (datetime == 0) and (len(radec) == 0):
        assert scan != 0, 'scan must be set when using datetime and radec'   # default scan value not valid

        logger.info('Calculating uvw for first integration of scan %d of source %s' % (scan, scans[scan]['source']))
        datetime = qa.time(qa.quantity(scans[scan]['startmjd'],'d'), form="ymd", prec=8)[0]
        sourcenum = [kk for kk in sources.keys() if sources[kk]['source'] == scans[scan]['source']][0]
        direction = me.direction('J2000', str(np.degrees(sources[sourcenum]['ra']))+'deg', str(np.degrees(sources[sourcenum]['dec']))+'deg')

    # secondary case is when datetime is also given
    elif (datetime != 0) and (len(radec) == 0):
        assert scan != 0, 'scan must be set when using datetime and radec'   # default scan value not valid
        assert '/' in datetime, 'datetime must be in yyyy/mm/dd/hh:mm:ss.sss format'

        logger.info('Calculating uvw at %s for scan %d of source %s' % (datetime, scan, scans[scan]['source']))
        sourcenum = [kk for kk in sources.keys() if sources[kk]['source'] == scans[scan]['source']][0]
        direction = me.direction('J2000', str(np.degrees(sources[sourcenum]['ra']))+'deg', str(np.degrees(sources[sourcenum]['dec']))+'deg')

    else:
        assert '/' in datetime, 'datetime must be in yyyy/mm/dd/hh:mm:ss.sss format'
        assert len(radec) == 2, 'radec must be (ra,dec) tuple in units of degrees'

        logger.info('Calculating uvw at %s in direction %s' % (datetime, direction))
        ra = radec[0]; dec = radec[1]
        direction = me.direction('J2000', str(ra)+'deg', str(dec)+'deg')

    # define metadata "frame" for uvw calculation
    sdm = sdmpy.SDM(sdmfile)
    telescopename = sdm['ExecBlock'][0]['telescopeName'].strip()
    logger.debug('Found observatory name %s' % telescopename)

    me.doframe(me.observatory(telescopename))
    me.doframe(me.epoch('utc', datetime))
    me.doframe(direction)

    # read antpos
    positions = [ant.position.strip().split(' ') for ant in sdm['Station'] if 'ANTENNA' in ant.type]
#    root = et.parse(os.path.join(sdmfile, 'Station.xml')).getroot()
#    positions = [rr.find('position').text.split(' ') for rr in root.iter('row') if 'ANTENNA' in rr.find('type').text]
    x = [float(positions[i][2]) for i in range(len(positions))]
    y = [float(positions[i][3]) for i in range(len(positions))]
    z = [float(positions[i][4]) for i in range(len(positions))]
    ants = me.position('itrf', qa.quantity(x, 'm'), qa.quantity(y, 'm'), qa.quantity(z, 'm'))

    # calc bl
    bls = me.asbaseline(ants)
    uvwlist = me.expand(me.touvw(bls)[0])[1]['value']

    # define new bl order to match sdm binary file bl order
    u = np.empty(len(uvwlist)/3); v = np.empty(len(uvwlist)/3); w = np.empty(len(uvwlist)/3)
    nants = len(ants['m0']['value'])
    ord1 = [i*nants+j for i in range(nants) for j in range(i+1,nants)]
    ord2 = [i*nants+j for j in range(nants) for i in range(j)]
    key=[]
    for new in ord2:
        key.append(ord1.index(new))
    for i in range(len(key)):
        u[i] = uvwlist[3*key[i]]
        v[i] = uvwlist[3*key[i]+1]
        w[i] = uvwlist[3*key[i]+2]

    return u, v, w

def read_metadata(sdmfile, scan=0, bdfdir=None):
    """ Parses XML files to get scan and source information.
    Returns tuple of dicts (scan, source).
    Optional arg scan can be used to speed up parsing for single scan.
    bdfstr in scan dict helps find BDFs with read_bdf (with special behavior for prearchive data.
    bdfdir is optional location to look for bdfs, if using pre-archive SDM.
    """

    if not bdfdir: bdfdir = '/lustre/evla/wcbe/data/bunker'

    sdmfile = sdmfile.rstrip('/')

    assert os.path.exists(sdmfile), 'Could not find sdmfile %s.' % sdmfile

    # if ASDMBinary directory exists, this is normal archive product. else assume we are on CBE.
    if os.path.exists(os.path.join(sdmfile, 'ASDMBinary')):
        location = 'archive'
    else:
        location = 'cbe'

    sdm = sdmpy.SDM(sdmfile)

    # read Scan.xml into dictionary also and make a list
    scandict = {}; sourcedict = {}
    if len(sdm['Scan']) > 1:    # workaround: conversion from MS to SDM tends to make scans into subscans of one large scan
        for i in range(len(sdm['Scan'])):
            row  = sdm['Scan'][i]
            scannum = int(row['scanNumber'])
            if scan in [0, scannum]:
                rowkey = [k for k in row.keys if k.lower() == 'numsubscan'][0]   # need to find key but caps rule changes between ALMA/VLA
                nsubs = int(row[rowkey])
                scanintents = row['scanIntent']
                intentstr = string.join(scanintents.strip().split(' ')[2:], ' ')
                startmjd = float(row['startTime'])*1.0E-9/86400.0           # start and end times in mjd ns
                endmjd = float(row['endTime'])*1.0E-9/86400.0
                try:
                    src = str(row["sourceName"])        # source name
                except:
                    logger.warn('Scan %d has no source name' % (len(scandict)+1))
                finally:
                    scandict[scannum] = {}
                    scandict[scannum]['source'] = src
                    scandict[scannum]['startmjd'] = startmjd
                    scandict[scannum]['endmjd'] = endmjd
                    scandict[scannum]['intent'] = intentstr
                    scandict[scannum]['nsubs'] = nsubs
                    scandict[scannum]['duration'] = endmjd-startmjd
                    scandict[scannum]['nints'] = int(sdm['Main'][i]['numIntegration'])
        
                try:
                    bdfstr = sdm['Main'][i]['dataUID'].replace(':', '_').replace('/', '_')
                except KeyError:
                    bdfstr = sdm['Main'][i]['dataOid'].replace(':', '_').replace('/', '_')

                if 'X1' in bdfstr: 
                    scandict[scannum]['bdfstr'] = None    # missing BDFs (bad or removed) have bdfstr='X1'
                else:
                    if location == 'archive':   # most use cases
                        scandict[scannum]['bdfstr'] = os.path.join(sdmfile, 'ASDMBinary', bdfstr)
                    elif location == 'cbe':  # bdfs stored remotly on CBE
                        scandict[scannum]['bdfstr'] = os.path.join(bdfdir, bdfstr)

                if scandict[scannum]['source'] not in [sourcedict[source]['source'] for source in sourcedict.iterkeys()]:
                    for row in sdm['Field']:
                        src = row['fieldName'].strip()
                        if src == scandict[scannum]['source']:
                            sourcenum = int(row["sourceId"])
                            direction = row["referenceDir"].strip()
                            (ra,dec) = [float(val) for val in direction.strip().split(' ')[3:]]  # skip first two values in string

                            # original version would add warning if two sources had different ra/dec. this makes one entry for every source
                            sourcedict[sourcenum] = {}
                            sourcedict[sourcenum]['source'] = src
                            sourcedict[sourcenum]['ra'] = ra
                            sourcedict[sourcenum]['dec'] = dec
                            break  # just take first instance, then escape

    elif ( (len(sdm['Scan']) == 1) and (len(sdm['Subscan']) > 1) ):
        logger.warn('Found only one scan with multiple subscans. Treating subscans as scans.')
        for row in sdm['Subscan']:
            scannum = int(row['subscanNumber'])
            if scan in [0, scannum]:
                startmjd = float(row['startTime'])*1.0E-9/86400.0           # start and end times in mjd ns
                endmjd = float(row['endTime'])*1.0E-9/86400.0
                scanintents = row['subscanIntent']
                if len(scanintents.strip().split(' ')) > 1:
                    intentstr = string.join(scanintents.strip().split(' ')[2:], ' ')
                else:
                    intentstr = scanintents

                try:
                    src = row["fieldName"].strip()        # source name
                except:
                    logger.warn('Scan %d has no source name' % (len(scandict)+1))
                finally:
                    scandict[scannum] = {}
                    scandict[scannum]['source'] = src
                    scandict[scannum]['intent'] = intentstr
                    scandict[scannum]['startmjd'] = startmjd
                    scandict[scannum]['endmjd'] = endmjd
                    scandict[scannum]['duration'] = endmjd-startmjd





            
    return [scandict, sourcedict]


"""
Class for reading Binary Data File, the raw format for SDM data.

Abuse the email.feedparser class to read the structure of a BDF binary file.
We remember where the binary blobs are so we can mmap them into numpy arrays
later.

f = open ('/path/to/bdf_file')
bdf = BDFData (f).parse ()
for i in xrange (bdf.n_integrations):
    c = bdf.get_data ('crossData.bin', i)
    nbl, nchan, npol = c.shape
    ....

The following arrays can be retrieved with the get_data() function:

crossData.bin: the basic cross-correlation data
  dtype: complex64
  shape: (nbaselines, nchans, npol)

autoData.bin: the basic auto-correlation data
  dtype: complex64
  shape: (nantennas, nchans, nfeeds=2)

flags.bin: flags on the auto and cross correlations
  dtype: uint32
  shape: (nbaselines + nantennas, nchans, npol)
  FIXME: I don't know how the baseline/antenna dimension should be indexed!
         Consult the BDF spec. Also, this will break if the BDF does not
         contain auto+cross data.

Shortcomings:

We hardcode the array axis orderings and which axis have non-unity size. This
could theoretically all change under us.

The BDF spec makes it sound like the different binary blobs are allowed to
have differing sizes -- the "size" attribute in the header is a maximum. If
this ever happens in practice, we're kind of dicked -- I don't see how we
can guess the datachunk size without just reading it in. Let's hope that
never happens.

BDF is little-endian as are x86 processors, so we ignore endianness issues.
"""

_datatypes = {
    'autoData.bin': np.complex64,
    'crossData.bin': np.complex64,
    'flags.bin': np.uint32,
}

nanttag = 'numAntenna'
basebandtag = 'baseband'

class BDFData (object):
    def __init__ (self, fp, bdfpkldir=''):
        """fp is an open, seekable filestream."""
        self.fp = fp
        self.mmdata = mmap.mmap (fp.fileno (), 0, mmap.MAP_PRIVATE, mmap.PROT_READ)
        if bdfpkldir:
            self.pklname = os.path.join(bdfpkldir, os.path.basename(self.fp.name) + '.pkl')
        else:
            self.pklname = ''

    def parse(self):
        """wrapper for original parse function. will read pkl with bdf info, if available."""

        if os.path.exists(self.pklname):        # check for pkl with binary info
            logger.info('Found bdf pkl file %s. Loading...' % (self.pklname))
            try:
                with open(self.pklname,'rb') as pkl:
                    (self.mimemsg, self.headxml, self.sizeinfo, self.binarychunks, self.n_integrations, self.n_antennas, self.n_baselines, self.n_basebands, self.n_spws, self.n_channels, self.crosspols) = pickle.load(pkl)
            except:
                logger.warning('Something went wrong. Parsing bdf directly...')
                self._parse()
        else:
            if self.pklname:
                logger.info('Could not find bdf pkl file %s.' % (self.pklname))
            self._parse()

        return self

    def _parse (self):
        """Parse the BDF mime structure and record the locations of the binary
        blobs. Sets up various data fields in the BDFData object."""

        feedparser = FeedParser (Message)
        binarychunks = {}
        sizeinfo = None
        self.fp.seek (0, 0)

        while True:
            data = self.fp.readline ()
            if not data:
                break

            feedparser.feed (data)

            skip = (data == '\n' and
                    len (feedparser._msgstack) == 3 and
                    feedparser._msgstack[-1].get_content_type () in ('application/octet-stream',
                                                                     'binary/octet-stream'))
            if skip:
                # We just finished reading the headers for a huge binary blob.
                # Time to remember where the data chunk is and pretend it doesn't
                # exist.
                msg = feedparser._msgstack[-1]
                ident = msg['Content-Location']
                assert ident.endswith ('.bin'), 'confusion #1 in hacky MIME parsing!'
                binarychunks[ident] = self.fp.tell ()
                if sizeinfo is None:
                    headxml, sizeinfo, tagpfx = _extract_size_info (feedparser)
                kind = ident.split ('/')[-1]
                assert kind in sizeinfo, 'no size info for binary chunk kind %s in MIME!' % kind
                self.fp.seek (sizeinfo[kind] + 1, 1) # skip ahead by data chunk size
                sample = self.fp.read (16)
                assert sample.startswith ('--MIME'), 'crap, unexpected chunk size in MIME parsing: %r' % sample
                self.fp.seek (-16, 1) # go back

        if headxml is None:
            raise RuntimeError ('never found any binary data')

        self.mimemsg = feedparser.close ()
        self.headxml = headxml
        self.sizeinfo = sizeinfo
        self.binarychunks = binarychunks

        # Compute some miscellaneous parameters that we'll need.
        self.n_integrations = len (self.mimemsg.get_payload ()) - 1
        self.n_antennas = int (headxml.find (tagpfx + nanttag).text)
        self.n_baselines = (self.n_antennas * (self.n_antennas - 1)) // 2

        ds = headxml.find (tagpfx + dstag)
        nbb = 0
        nspw = 0
        nchan = 0
        crosspolstr = None

        for bb in ds.findall (tagpfx + basebandtag):
            nbb += 1

            for spw in bb.getchildren ():
                nspw += 1
                nchan += int (spw.get ('numSpectralPoint'))

                if crosspolstr is None:
                    crosspolstr = spw.get ('crossPolProducts')
                elif spw.get ('crossPolProducts') != crosspolstr:
                    raise Exception ('can only handle spectral windows with identical cross pol products')

        self.n_basebands = nbb
        self.n_spws = nspw
        self.n_channels = nchan
        self.crosspols = crosspolstr.split ()
        self.n_pols = len(self.crosspols)

        # if bdf info pkl not present, write it
        if os.path.exists(os.path.dirname(self.pklname)) and self.pklname and (not os.path.exists(self.pklname)):
            logger.info('Writing bdf pkl info to %s...' % (self.pklname))
            with open(self.pklname,'wb') as pkl:
                # Compute some miscellaneous parameters that we'll need.
                pickle.dump( (self.mimemsg, self.headxml, self.sizeinfo, self.binarychunks, self.n_integrations, self.n_antennas, self.n_baselines, self.n_basebands, self.n_spws, self.n_channels, self.crosspols), pkl, protocol=-1)

        return self # convenience

    def get_data (self, datakind, integnum):
        """Given an integration number (0 <= integnum < self.n_integrations) and a
        data kind ('crossData.bin', 'autoData.bin'), memory-map the corresponding data
        and return a wrapping numpy array."""

        if integnum < 0 or integnum >= self.n_integrations:
            raise ValueError ('illegal integration number %d' % integnum)

        size = self.sizeinfo.get (datakind)
        if size is None:
            raise ValueError ('unrecognized data kind "%s"' % datakind)

        dtype = _datatypes[datakind]
        key = '/%d/%s' % (integnum + 1, datakind) # numbers are 1-based here

        for ident, offset in self.binarychunks.iteritems ():
            if ident.endswith (key):
                break
        else:
            # Gets executed if we don't break out of the loop.
            raise ValueError ('can\'t find integration #%d of kind %s in BDF'
                              % (integnum, datakind))

        dslice = self.mmdata[offset:offset+size]
        data = np.fromstring (dslice, dtype=dtype)

        if datakind == 'crossData.bin':
            data = data.reshape ((self.n_baselines, self.n_channels, len (self.crosspols)))
        elif datakind == 'autoData.bin':
            data = data.reshape ((self.n_antennas, self.n_channels, 2))
        elif datakind == 'flags.bin':
            data = data.reshape ((self.n_baselines + self.n_antennas, self.n_channels,
                                  len (self.crosspols)))

        return data


tagprefixes = ['{http://Alma/XASDM/sdmbin}', '']
dstag = 'dataStruct'
cdtag = 'crossData'
adtag = 'autoData'
fgtag = 'flags'

def _extract_size_info (feedparser):
    # This parses the XML of the header section

    text = feedparser._msgstack[0].get_payload ()[0].get_payload ()
    headxml = et.fromstring (text)

    # The XML may or may not have an xmlns attribute which manifests itself
    # as a prefix to the tags we need to use.

    sizeinfo = {}

    for tp in tagprefixes:
        dselem = headxml.find (tp + dstag)
        if dselem is not None:
            break
    else:
        raise RuntimeError ('cannot find dataStruct item in any known XML namespace')

    # Now pull out the dataStruct bit and chunk size info. We return
    # sizes in bytes, not data elements.

    e = dselem.find (tp + cdtag)
    if e is not None:
        sizeinfo['crossData.bin'] = 4 * int (e.attrib['size'])

    e = dselem.find (tp + adtag)
    if e is not None:
        sizeinfo['autoData.bin'] = 4 * int (e.attrib['size'])

    e = dselem.find (tp + fgtag)
    if e is not None:
        sizeinfo['flags.bin'] = 4 * int (e.attrib['size'])

    # ... could fill in more if needed ...

    return headxml, sizeinfo, tp

