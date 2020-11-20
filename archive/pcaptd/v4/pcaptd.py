#!/usr/bin/env python 
#
# Copyright (c) 2013, Arista Networks, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#  - Redistributions of source code must retain the above copyright notice,
# this list of conditions and the following disclaimer.
#  - Redistributions in binary form must reproduce the above copyright
# notice, this list of conditions and the following disclaimer in the
# documentation and/or other materials provided with the distribution.
#  - Neither the name of Arista Networks nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
# 
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL ARISTA NETWORKS
# BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR
# BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE
# OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN
# IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# PCAP Timestamp Decoder
#
#    Version 4.1 16/03/2015
#    Written by: 
#       John Peach, Arista Networks
#       Matt Murray, Arista Networks
#       Andrei Dvornic, Arista Networks
#       Phil Harrison, Arista Networks
#
#    Revision history:
#       1.0 - initial release
#       1.1 - added key frame support
#           - added command line options
#       2.0 - added support for devId-VLAN mapping
#       2.1 - added support for replacing pcap timestamp with decoded UTC
#           - improved UTC decoding accuracy (account for congestion)
#       2.2 - added support for untagged traffic
#           - added warning for skipped packets
#           - added error message on missing deviceId configuration
#       3.0 - simplified decoding algorithm
#           - decode error vs. previous versions: max 150ns in 0.04% of cases
#       3.1 - cover all corner cases
#       3.2 - added support for decoding pcaps which include FCS
#       3.3 - enhanced help message
#       4.0 - added nanosecond precision for output pcap
#       4.1 - bug fixes

'''
   DESCRIPTION
      PCAP Timestamp Decoder enables users to decode the timestamps
      applied by the Arista 7150 series switches.
   INSTALLATION
      Requirements: 
         - Python 2.6 or later: http://www.python.org/
         - dpkt: http://code.google.com/p/dpkt/
      In order to install PCAP Timestamp Decoder, copy 'pcaptd' to
      your filesystem.
      Then define the mapping between the device id (in the key frames)
      and the VLANs corresponding to the packets timestamped through
      that device in the highlighted section below (at the beginning
      of the script).
      Once the mapping is configured, the PCAP Timestamp Decoder can
      then be started using:
      
         (bash:root)# <path-to-script>/pcaptd [<options>] <input_pcap>
   CONFIGURATION 
      In order to show UTC decode details, use the '--utc-details'
      option. By default, stats are sent to stdout. In order to print
      them to a file, use the '--write-details-to-file' option (this
      is recommended for large input pcap files).
      The following options can be used in order to control the
      details generated by the script:
         -d, --delta           show delta between consecutive packets
         -f, --fcs             input pcap includes FCS
         -p, --pcap-timestamps show pcap timestamps
         -r, --recover-utc     show UTC time
         -s, --src-ip          show source IP
         -t, --ticks           show hw timestamps as ticks
      The 'Notes' column can have one of the following values:
        - R:       rollover
        - KEY:     key frame
     The script can create a new pcap file, identical with the input
     one, except that the timestamp in the pcap is replaced by the
     decoded UTC value. In order to enable this behaviour please use
     the '--output-file' option.  Note that the entries which cannot
     be decoded will show up with a timestamp of 0 in the output file.
     Moreover, note that the timestamp format of the new pcap file
     is using nanosecond precision; this means that older versions
     of Whireshark (earlier than 1.0.5 ) might not be able to decode 
     it. For more on this, see:
         http://wiki.wireshark.org/Development/LibpcapFileFormat
   
   COMPATIBILITY 
      Version 4.1 has been developed and tested against
      Python 2.7 on MacOS, but should work on any other operating
      system supporting Python 2.6 or later. Please reach out to
      support@aristanetworks.com for assistance if needed.
   LIMITATIONS
      The tool tries to detect a counter rollover (by checking if a
      subsequent timestamp has a lower tick value than it
      predecessor). However, this mechanism does not uncover periods
      where the inter-timestamp delta is > 6.15s. This can result in
      undetected rollovers, if:
            6.15s < delta < (2 * 6.15 - previous timestamp)
      Multiple rollovers cannot be tracked and may go undetected.
      When the '--output-file' option is used, the resulting file's
      timestamp accuracy will be microseconds. This is because libpcap
      does not support nanosecond precision.  If nanosecond precision
      is desired, use the '--utc-details' option.
      One or two keyframes in advance are required for decoding UTC in
      a packet. If they are not available, then decoding the
      timestamp in packets might not be possible.
      The decoder assumes that the timestamp is located in the last
      four bytes of the frame. If the input file includes the Ethernet
      FCS (typically stripped by the NIC driver) this will result
      in parsing the wrong part of the frame as the timestamp. Use the
      --fcs option to ignore the last four bytes and consider the
      previous bytes as timestamp.
'''

vlanToDevId = {}

# ------------------------------------------------------------
# !!! INSERT VLAN->deviceId mapping below !!!
# ------------------------------------------------------------
# vlanToDevId[ <vlan> ] = <devId>
# e.g. 
#    VLAN10 timestamped through device 1
#    VLAN20 and VLAN30 timestamped through device 2
#
#    vlanToDevId[ 10 ] = 1
#    vlanToDevId[ 20 ] = 2
#    vlanToDevId[ 30 ] = 2
#
# If the inspected traffic is untagged and timestamped on a single 
# switch, you can use:
#
#    vlanToDevId[ '*' ] = <devId>
#
# e.g.
#    Untagged traffic timestamped through device 1
#
vlanToDevId[ '*' ] = 888
#-------------------------------------------------------------

import struct
import dpkt
import optparse
import socket
import sys
import time

from datetime import datetime
from collections import namedtuple

from dpkt import pcap
pcap.TCPDUMP_MAGIC = 0xa1b23c4dL

Timestamp = namedtuple( 'Timestamp', 'pcapTs pcapDelta hwTicks ' 
                        'hwDelta utc utcDelta rollover' )
Pcap = namedtuple( 'Pcap', 'timestamp keyframe ' 
                   'srcIp vlanOrDevId' )
KeyframeData = namedtuple( 'KeyframeData', 'asicTs utc' )

# Based on the clock rate (350Mhz)
TICK_LENGTH = 20.0/7.0
MAX_TICKS = 2**31 - 1

# { deviceId : <value> }
prevHwTicks = {}
prevPcapTime = {}
prevUtc = {}

#                 two kf in adv.  prev. kf.
# { <deviceId> : [ KeyframeData, KeyframeData ] }
keyframes = {}

class VLANEthernet( dpkt.ethernet.Ethernet ):
   __hdr__ = (
      ( 'dst', '6s', '' ),
      ( 'src', '6s', '' ),
      ( '__VLAN__', 'H', dpkt.ethernet.ETH_TYPE_8021Q ),
      ( 'vlan', 'H', 1 ),
      ( 'type', 'H', dpkt.ethernet.ETH_TYPE_IP )
      )
   _typesw = {}

class PcapNGFileHdr( dpkt.pcap.FileHdr ):
   """pcap file header."""
   __hdr__ = (
      ( 'magic', 'I', 0xa1b23c4dL ),
      ( 'v_major', 'H', dpkt.pcap.PCAP_VERSION_MAJOR ),
      ( 'v_minor', 'H', dpkt.pcap.PCAP_VERSION_MINOR ),
      ( 'thiszone', 'I', 0 ),
      ( 'sigfigs', 'I', 0 ),
      ( 'snaplen', 'I', 1500 ),
      ( 'linktype', 'I', 1 ),
      )

class PcapNGWriter( dpkt.pcap.Writer ):
   def __init__( self, fileobj, 
                 snaplen=1500, linktype=dpkt.pcap.DLT_EN10MB ):
      self.__f = fileobj
      fh = PcapNGFileHdr( naplen=snaplen, linktype=linktype )
      self.__f.write( str( fh ) )
      
   def writepkt( self, pkt, ts=None ):
      if ts is None:
         ts = time.time()
      s = str( pkt )
      n = len( s )
      ph = dpkt.pcap.PktHdr( tv_sec=int( ts ),
                             # nanosecond precision                     
                             tv_usec=int( ( float( ts ) - 
                                            int( ts ) ) * 1000000000.0 ),
                             caplen=n, len=n )
      self.__f.write( str( ph ) )
      self.__f.write( s )
            
   def close(self):
      self.__f.close()

def _untaggedTraffic():
   return len( vlanToDevId ) == 1 and '*' in vlanToDevId

def _devId( key ):
   if _untaggedTraffic():
      key = '*'
   return vlanToDevId[ key ]

def _printTimestamps( pcapData, showPcap, showUTC, showTicks, showDelta, 
                      showSourceIp, filename=None ):
   units = 'ticks'
   if not showTicks:
      units = 'ns'

   header = [ 'PCAP tstamp(s)'.rjust( 18 ) 
                 if showPcap else '',
              'PCAP delta(ns)'.rjust( 18 ) 
                 if showPcap and showDelta else '',
              ( 'HW tstamp(%s)' % units ).rjust( 18 ),
              ( 'HW delta(%s)' % units ).rjust( 18 ) 
                 if showDelta else '',
              'UTC(ns)'.rjust( 20 ) 
                 if showUTC else '',
              'UTC delta(ns)'.rjust( 15 ) 
                 if showUTC and showDelta else '',
              'UTC'.rjust( 30 ) 
                 if showUTC and showUTC else '',
              'Source IP'.rjust( 15 ) 
                 if showSourceIp else '',
              'VLAN/DeviceId(VLANs)'.rjust( 20 ),
              'Note'.rjust( 5 ) ]

   if filename:
      try:
         f = open( filename, 'w' )
      except IOError, e:
         sys.exit( '\nERROR: Unable to open file %s: %s\n' % 
                   ( filename, str( e ) ) )

      f.write( ' '.join( header ) + '\n' )
   else:
      print( ' '.join( header ) )

   for entry in pcapData:
      note = ''
      if entry.timestamp.rollover:
         note = 'R'
      elif entry.keyframe:
         note = 'KEY'

      utcString = datetime.utcfromtimestamp( 
                     entry.timestamp.utc / 10.0**9 ).strftime( 
                     '%Y-%m-%d %H:%M:%S.%f' )

      if showTicks:
         hwString = '%d' % entry.timestamp.hwTicks
         hwDeltaString = '%d' % entry.timestamp.hwDelta
      else: 
         hwString = '%1.2f' % ( entry.timestamp.hwTicks * TICK_LENGTH )
         hwDeltaString = '%1.2f' % ( entry.timestamp.hwDelta * TICK_LENGTH )
         
      if entry.keyframe:
         vlanOrDevIdString = '%d' % entry.vlanOrDevId
         if not _untaggedTraffic():
            vlanOrDevIdString += '(%s)' % ( 
               ','.join( [ str( x ) 
                           for x in vlanToDevId 
                           if vlanToDevId[ x ] == entry.vlanOrDevId ] ) )
         else:
            vlanOrDevIdString += '(untagged)'
      else:
         vlanOrDevIdString = 'VLAN%s' % entry.vlanOrDevId \
                             if entry.vlanOrDevId is not None \
                             else 'Untagged'

      row = [ ( '%1.6f' % entry.timestamp.pcapTs 
                if entry.timestamp.pcapTs else '' ).rjust( 18 ) 
                   if showPcap else '',
              ( '%1.6f' % entry.timestamp.pcapDelta 
                if entry.timestamp.pcapDelta else '' ).rjust( 18 ) 
              if showPcap and showDelta else '',
              hwString.rjust( 18 ),
              ( hwDeltaString if entry.timestamp.hwDelta else '' ).rjust( 18 )
              if showDelta else '',
              ( '%d' % entry.timestamp.utc 
                if entry.timestamp.utc else '' ).rjust( 20 ) 
              if showUTC else '',
              ( ( '%1.0f' % entry.timestamp.utcDelta 
                  if entry.timestamp.utcDelta else '' ) 
                if entry.timestamp.utc else '' ).rjust( 15 ) 
              if showUTC  and showDelta else '',
              ( utcString if entry.timestamp.utc else '' ).rjust( 30 ) 
              if showUTC else '',
              ( socket.inet_ntoa( entry.srcIp ) 
                if entry.srcIp else '' ).rjust( 15 ) 
              if showSourceIp else '',
              vlanOrDevIdString.rjust( 20 ),
              note.rjust( 5 ) ]

      if filename:
         f.write( ' '.join( row ) + '\n' )
      else:
         print( ' '.join( row ) )

   if filename:
      f.close()

def _decodeValue( timestamp, keyframe ):
   if keyframe is None:
      return 0

   diff = timestamp - keyframe.asicTs
   if diff < 0:
      diff += MAX_TICKS
   return keyframe.utc + long( round( diff * TICK_LENGTH ) )

def _decodeTimestamps( pcapTs, hwTicks, devId ):
   kf = None
   prevKf = None

   kframes = len( keyframes[ devId ] )
   if kframes > 0:
      assert kframes < 3
      kf = keyframes[ devId ][ -1 ]
      if kframes == 2:
         prevKf = keyframes[ devId ][ 0 ]

   k1 = kf.asicTs if kf else None
   utc = 0
   if k1:
      if k1 < hwTicks:
         if hwTicks - k1 > 2**31 / 2:
            utc = _decodeValue( hwTicks, prevKf )
         else:
            utc = _decodeValue( hwTicks, kf )
      elif k1 > hwTicks:
         if k1 - hwTicks < 2**31 / 2:
            utc = _decodeValue( hwTicks, prevKf )            
         else:
            utc = _decodeValue( hwTicks, kf )
      else:
         utc = kf.utc

   rollover = False
   hwDelta = 0
   pcapDelta = 0
   utcDelta = 0

   if devId in prevHwTicks:
      hwDelta = hwTicks - prevHwTicks[ devId ]
      if hwDelta < 0:
         hwDelta += MAX_TICKS
         rollover = True
   prevHwTicks[ devId ] = hwTicks

   if devId in prevPcapTime and prevPcapTime[ devId ]:
      pcapDelta = ( pcapTs - prevPcapTime[ devId ] ) * 10**9
   prevPcapTime[ devId ] = pcapTs

   if utc and devId in prevUtc and prevUtc[ devId ]:
      utcDelta = ( utc - prevUtc[ devId ] )
   prevUtc[ devId ] = utc

   return Timestamp( pcapTs, pcapDelta, hwTicks, hwDelta, 
                     utc, utcDelta, rollover )

def _readPcap( inputFile, outputFile, fcs ):
   try:
      inFile = open( inputFile, 'rb' )
   except IOError, e:
      sys.exit( '\nERROR: Unable to open file %s: %s\n' % 
                ( inputFile, str( e ) ) )

   pcapData = []
   pcapReader = dpkt.pcap.Reader( inFile )
   pcapInfo = [ x for x in pcapReader ]

   if outputFile:
      try:
         outFile = open( outputFile, 'w' )
      except IOError, e:
         sys.exit( '\nERROR: Unable to open file %s: %s\n' % 
                   ( outputFile, str( e ) ) )
      pcapWriter = PcapNGWriter( outFile )

   for devId in set( vlanToDevId.values() ):
      keyframes[ devId ] = []

   print( '\nDecoding timestamps...\n' )
   for pcapEntry in pcapInfo:
      pcapTs, buf = pcapEntry
      eth = dpkt.ethernet.Ethernet( buf )

      # Key frame
      if eth.type == 2048 and eth.ip.p == 253 and eth.ip.ttl == 64:
         (asicTime, utc, deviceId) = \
             struct.unpack('>2Q%ixH4x' % ( len( eth.ip.data ) - 22 ), 
                           eth.ip.data)
         if deviceId not in keyframes:
            sys.exit( 'ERROR: Keyframe from device %d seen in the capture, '
                      'but device not configured in the '
                      'VLAN->deviceId mapping! ' % 
                      deviceId )
         if len( keyframes[ deviceId ] ) == 2:
            keyframes[ deviceId ] = keyframes[ deviceId ][ 1: ]

         hwTicks = asicTime & 0x7FFFFFFF
         keyframes[ deviceId ].append( KeyframeData( hwTicks, utc ) )

         pcapData.append( Pcap( Timestamp( pcapTs=pcapTs, 
                                           pcapDelta=0, 
                                           hwTicks=hwTicks,
                                           hwDelta=0, 
                                           utc=utc, 
                                           utcDelta=0, 
                                           rollover=False ), 
                                keyframe=True, 
                                srcIp=eth.ip.src, 
                                vlanOrDevId=deviceId ) )
         if outputFile:
            pcapWriter.writepkt( buf, ts=0 )
      else:
         vlan = None
         if not _untaggedTraffic():
            # Ignore packets which don't belong to the configured VLANs
            try:
               vlan = VLANEthernet( buf ).vlan
            except Exception, e:
               continue

            if vlan not in vlanToDevId:
               print( 'Skipping packet because VLAN %s not configured in '
                      'VLAN->deviceId mapping!' % vlan )
               continue

         if fcs:
            (stamp,) = struct.unpack('>L', buf[ -8 : -4 ])
         else:
            (stamp,) = struct.unpack('>L', buf[ -4 : ])

         hwTicks = ( ( stamp & 0xffffff00 ) >> 1 ) | ( stamp & 0x7f )
         devId = _devId( vlan )
         ts =  _decodeTimestamps( pcapTs, hwTicks, devId )
         pcapData.append( Pcap( timestamp=ts, 
                                keyframe=False, 
                                srcIp=eth.ip.src if eth.type == 2048 else None, 
                                vlanOrDevId=vlan ) )
         if outputFile:
            pcapWriter.writepkt( buf, ts=float( ts.utc ) / 10**9 )

   if outputFile:
      pcapWriter.close()

   return pcapData

def main():
   # Create help string and parse cmd line
   usage = 'usage: %prog [options] <input-filename>'
   op = optparse.OptionParser(usage=usage)
   op.add_option( '-d', '--delta', dest='delta', 
                  action='store_true', help='show delta between consecutive '
                                            'packets in UTC decode details '
                                            '(requires -u)' )
   op.add_option( '-f', '--fcs', dest='fcs', 
                  action='store_true', help='input pcap includes FCS' )
   op.add_option( '-p', '--pcap-timestamps', dest='pcap', 
                  action='store_true', 
                  help='show pcap timestamps in UTC decode details '
                       '(requires -u)' )
   op.add_option( '-r', '--recover-utc', dest='utc', 
                  action='store_true', 
                  help='show UTC time in UTC decode details '
                       '(requires -u)' )
   op.add_option( '-o', '--output-file', dest='pcapFilename', 
                  action='store', help='decode UTC to pcap' )
   op.add_option( '-s', '--src-ip', dest='src', 
                  action='store_true', 
                  help='show source IP in UTC decode details '
                       '(requires -u)' )
   op.add_option( '-t', '--ticks', dest='ticks', 
                  action='store_true', 
                  help='show hw timestamps as ticks in UTC decode details '
                       '(requires -u)' )
   op.add_option( '-u', '--utc-details', dest='utcDetails', 
                  action='store_true', 
                  help='show/print-to-file UTC decode details' )
   op.add_option( '-w', '--write-details-to-file', dest='detailsFilename', 
                  action='store', help='output file for UTC decode details' )

   opts, arguments = op.parse_args()

   if not vlanToDevId:
      sys.exit( 'ERROR: Please configure the deviceId->VLAN mapping at the '
                'top of the script!' )
 
   # Check cmd line options
   if not arguments:
      op.error( 'You need to specify an input filename.' )
   if len( arguments ) > 1:
      op.error( 'Too many input arguments.' )
   if opts.detailsFilename and not opts.utcDetails:
      op.error( 'Output filename specified, but generating UTC decode '
                'details is not enabled. Please use the "-u" option in '
                'order to enable that.' )

   pcapData = _readPcap( arguments[ 0 ],
                         opts.pcapFilename,
                         opts.fcs )
   if opts.utcDetails:
      _printTimestamps( pcapData, opts.pcap, opts.utc, opts.ticks, 
                        opts.delta, opts.src, opts.detailsFilename )

if __name__ == '__main__':
   main()
