#!/usr/bin/env python

#
# Elijah: Cloudlet Infrastructure for Mobile Computing
# Copyright (C) 2011-2012 Carnegie Mellon University
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of version 2 of the GNU General Public License as published
# by the Free Software Foundation.  A copy of the GNU General Public License
# should have been distributed along with this program in the file
# LICENSE.GPL.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
# for more details.
#

import os
import sys
import struct
import tool
import mmap
import vmnetx
from progressbar import AnimatedProgressBar
from delta import DeltaItem
from delta import DeltaList
from hashlib import sha256
from operator import itemgetter
from optparse import OptionParser

#GLOBAL
EXT_RAW = ".raw"
EXT_META = ".meta"

class MemoryError(Exception):
    pass


class Memory(object):
    HASH_FILE_MAGIC = 0x1145511a
    HASH_FILE_VERSION = 0x00000001

    # kvm-qemu constant (version 1.0.0)
    RAM_MAGIC = 0x5145564d
    RAM_VERSION = 0x00000003
    RAM_PAGE_SIZE    =  (1<<12)
    RAM_ID_STRING       =   "pc.ram"
    RAM_ID_LENGTH       =   len(RAM_ID_STRING)
    RAM_PAGE_SIZE       =   1<<12 # 4K bytes
    RAM_SAVE_FLAG_COMPRESS = 0x02
    RAM_SAVE_FLAG_MEM_SIZE = 0x04
    RAM_SAVE_FLAG_PAGE     = 0x08
    RAM_SAVE_FLAG_RAW      = 0x40
    RAM_SAVE_FLAG_EOS      = 0x10
    RAM_SAVE_FLAG_CONTINUE = 0x20
    BLK_MIG_FLAG_EOS       = 0x02

    def __init__(self):
        self.hash_list = []
        self.raw_file = ''
        self.raw_mmap = None
        self.footer_data = None

    @staticmethod
    def _seek_string(f, string):
        # return: index of end of the found string
        start_index = f.tell()
        memdata = ''
        while True:
            memdata = f.read(4096)
            if not memdata:
                raise MemoryError("Cannot find %s from give memory snapshot" % Memory.RAM_ID_STRING)

            ram_index = memdata.find(Memory.RAM_ID_STRING)
            if ram_index:
                if ord(memdata[ram_index-1]) == len(string):
                    position = start_index + ram_index
                    f.seek(position)
                    return position
            start_index += len(memdata)

    def _get_mem_hash(self, fin, start_offset, end_offset, hash_list, **kwargs):
        # kwargs
        #  diff: compare hash_list with self object
        #  print_out: log/process output 
        diff = kwargs.get("diff", None)
        print_out = kwargs.get("print_out", open("/dev/null", "w+b"))
        print_out.write("[INFO] Get hash list of memory page\n")
        prog_bar = AnimatedProgressBar(end=100, width=80, stdout=print_out)

        fin.seek(start_offset)
        total_size = end_offset-start_offset
        ram_offset = 0
        while total_size != ram_offset:
            data = fin.read(Memory.RAM_PAGE_SIZE)
            if not diff:
                hash_list.append((ram_offset, self.RAM_PAGE_SIZE, sha256(data).digest()))
            else:
                # compare input with hash or corresponding base memory, save only when it is different
                self_hash_value = self.hash_list[ram_offset/self.RAM_PAGE_SIZE][2]
                if self_hash_value != sha256(data).digest():
                    #get xdelta comparing self.raw
                    source_data = self.get_raw_data(ram_offset, self.RAM_PAGE_SIZE)
                    #save xdelta as DeltaItem only when it gives smaller
                    try:
                        patch = tool.diff_data(source_data, data, 2*len(source_data))
                        if len(patch) < len(data):
                            delta_item = DeltaItem(ram_offset, self.RAM_PAGE_SIZE, 
                                    hash_value=sha256(data).digest(),
                                    ref_id=DeltaItem.REF_XDELTA,
                                    data_len=len(patch),
                                    data=patch)
                        else:
                            raise IOError("xdelta3 patch is bigger than origianl")
                    except IOError as e:
                        #print "[INFO] xdelta failed, so save it as raw (%s)" % str(e)
                        delta_item = DeltaItem(ram_offset, self.RAM_PAGE_SIZE, 
                                hash_value=sha256(data).digest(),
                                ref_id=DeltaItem.REF_RAW,
                                data_len=len(data),
                                data=data)
                    hash_list.append(delta_item)

                # memory overusage protection
                if len(hash_list) > Memory.RAM_PAGE_SIZE*1000000: # 400MB for hashlist
                    raise MemoryError("possibly comparing with wrong base VM")
            ram_offset += len(data)
            # print progress bar for every 100 page
            if (ram_offset % (Memory.RAM_PAGE_SIZE*100)) == 0:
                prog_bar.set_percent(100.0*ram_offset/total_size)
                prog_bar.show_progress()
        prog_bar.finish()

    @staticmethod
    def _seek_to_end_of_ram(fin):
        # get ram total length
        position = Memory._seek_string(fin, Memory.RAM_ID_STRING)
        memory_start_offset = position-(1+8)
        fin.seek(memory_start_offset)
        total_mem_size = long(struct.unpack(">Q", fin.read(8))[0])
        if total_mem_size & Memory.RAM_SAVE_FLAG_MEM_SIZE == 0:
            raise MemoryError("invalid header format: no total memory size")
        total_mem_size = total_mem_size & ~0xfff

        # get ram length information
        read_ramlen_size = 0
        ram_info = dict()
        while total_mem_size > read_ramlen_size:
            id_string_len = ord(struct.unpack(">s", fin.read(1))[0])
            id_string, mem_size = struct.unpack(">%dsQ" % id_string_len,\
                    fin.read(id_string_len+8))
            ram_info[id_string] = {"length":mem_size}
            read_ramlen_size += mem_size

        read_mem_size = 0
        while total_mem_size != read_mem_size:
            raw_ram_flag = struct.unpack(">Q", fin.read(8))[0]
            if raw_ram_flag & Memory.RAM_SAVE_FLAG_EOS:
                raise MemoryError("Not Fully load yet")
                break
            if raw_ram_flag & Memory.RAM_SAVE_FLAG_RAW == 0:
                raise MemoryError("invalid ram save flag raw")

            id_string_len = ord(struct.unpack(">s", fin.read(1))[0])
            id_string = struct.unpack(">%ds" % id_string_len, fin.read(id_string_len))[0]
            padding_len = fin.tell() & (Memory.RAM_PAGE_SIZE-1)
            padding_len = Memory.RAM_PAGE_SIZE-padding_len
            fin.read(padding_len)

            cur_offset = fin.tell()
            block_info = ram_info.get(id_string)
            if not block_info:
                raise MemoryError("Unknown memory block : %s", id_string)
            block_info['offset'] = cur_offset
            memory_size = block_info['length']
            fin.seek(cur_offset + memory_size)
            read_mem_size += memory_size

        return fin.tell(), ram_info

    def _load_file(self, filepath, **kwargs):
        # Load KVM Memory snapshot file and 
        # extract hashlist of each memory page while interpreting the format
        # filepath = file path of the loading file
        # kwargs
        #  diff_file: compare filepath(modified ram) with self hash
        #  print_out: log/process output 
        #
        ####
        # |----------------------------------------------|---------------|
        # 0        (FALG_RAW of pc.ram)     (end of all ram data)      EOF
        #            hashing memory                            footer
        ####
        diff = kwargs.get("diff", None)
        print_out = kwargs.get("print_out", open("/dev/null", "w+b"))
        if diff and len(self.hash_list) == 0:
            raise MemoryError("Cannot compare give file this self.hashlist")

        # Sanity check
        fin = open(filepath, "rb")
        libvirt_mem_hdr = vmnetx._QemuMemoryHeader(fin)
        libvirt_mem_hdr.seek_body(fin)
        libvirt_header_len = fin.tell()
        if libvirt_header_len % Memory.RAM_PAGE_SIZE != 0:
            # TODO: need to modify libvirt migration file header 
            # in case it is not aligned with memory page size
            raise MemoryError("libvirt memory header is not aligned with PAGE SIZE(%ld)" % libvirt_header_len)

        # get hash of memory area
        fin.seek(libvirt_header_len)
        hash_list = []
        ram_end_offset, ram_info = Memory._seek_to_end_of_ram(fin)
        if ram_end_offset % Memory.RAM_PAGE_SIZE != 0:
            print "end offset: %ld" % (ram_end_offset)
            raise MemoryError("ram header+data is not aligned with page size")
        self._get_mem_hash(fin, 0, ram_end_offset, hash_list, diff=diff, print_out=print_out)

        # save footer data
        # cur_offset = fin.tell(); fin.seek(0, 2); total = fin.tell(); fin.seek(cur_offset)
        footer_data = ''
        while True:
            read_data = fin.read(Memory.RAM_PAGE_SIZE)
            if not read_data:
                break
            footer_data += read_data

        if diff:
            return hash_list, footer_data
        else:
            self.footer_data = footer_data
            return hash_list, self.footer_data

    @staticmethod
    def import_from_metafile(meta_path, raw_path):
        # Regenerate KVM Base Memory DS from existing meta file
        if (not os.path.exists(raw_path)) or (not os.path.exists(meta_path)):
            msg = "Cannot import from hash file, No raw file at : %s" % raw_path
            raise MemoryError(msg)

        memory = Memory()
        memory.raw_file = open(raw_path, "rb")
        fd = open(meta_path, "rb")

        # MAGIC & VERSION
        magic, version = struct.unpack("!qq", fd.read(8+8))
        if magic != Memory.HASH_FILE_MAGIC:
            msg = "Hash file magic number(%ld != %ld) does not match" % (magic, Memory.HASH_FILE_MAGIC)
            raise IOError(msg)
        if version != Memory.HASH_FILE_VERSION:
            msg = "Hash file version(%ld != %ld) does not match" % \
                    (version, Memory.HASH_FILE_VERSION)
            raise IOError(msg)

        # Read Footer data
        footer_data_len = struct.unpack("!q", fd.read(8))[0]
        memory.footer_data = fd.read(footer_data_len)

        # Read Hash Item List
        while True:
            data = fd.read(8+4+32) # start_offset, length, hash
            if not data:
                break
            value = tuple(struct.unpack("!qI32s", data))
            memory.hash_list.append(value)
        fd.close()
        return memory

    @staticmethod
    def pack_hashlist(hash_list):
        # pack hash list
        original_length = len(hash_list)
        hash_list = dict((x[2], x) for x in hash_list).values()
        print "[Debug] hashlist is packed: from %d to %d : %lf" % \
                (original_length, len(hash_list), 1.0*len(hash_list)/original_length)
        
    def export_to_file(self, f_path):
        fd = open(f_path, "wb")
        # Write MAGIC & VERSION
        fd.write(struct.pack("!q", Memory.HASH_FILE_MAGIC))
        fd.write(struct.pack("!q", Memory.HASH_FILE_VERSION))

        # Write Footer data
        fd.write(struct.pack("!q", len(self.footer_data)))
        fd.write(self.footer_data)
        # Write hash item list
        for (start_offset, length, data) in self.hash_list:
            # save it as little endian format
            row = struct.pack("!qI32s", start_offset, length, data)
            fd.write(row)
        fd.close()

    def get_raw_data(self, offset, length):
        # retrieve page data from raw memory
        if not self.raw_mmap:
            self.raw_mmap = mmap.mmap(self.raw_file.fileno(), 0, prot=mmap.PROT_READ)
        return self.raw_mmap[offset:offset+length]

    def get_modified(self, new_kvm_file):
        # get modified pages, footer delta
        hash_list, modi_footer_data = self._load_file(new_kvm_file, diff=True, print_out=sys.stdout)
        try:
            print "footer info %ld %ld" % (len(modi_footer_data), len(self.footer_data))
            print "footer info %s %s" % (type(modi_footer_data), type(self.footer_data))
            footer_delta = tool.diff_data(self.footer_data, modi_footer_data, 2*len(self.footer_data))
        except IOError as e:
            sys.stderr.write("[WARNING] xdelta failed, so save it as raw (%s)\n" % str(e))
            footer_delta = modi_footer_data

        print "[INFO] footer size(%ld->%ld)" % \
               (len(modi_footer_data), len(footer_delta))
        return footer_delta, hash_list
    
    def get_delta(self, delta_list, ref_id):
        if len(delta_list) == 0 or type(delta_list[0]) != DeltaItem:
            raise MemoryError("Need list of DeltaItem")

        # make self as a unique list for better comparison performance
        # TODO: Avoid live packing
        Memory.pack_hashlist(self.hash_list)
        self.hash_list.sort(key=itemgetter(2)) # sort by hash value
        delta_list.sort(key=itemgetter('hash_value')) # sort by hash value

        matching_count = 0
        s_index = 0
        index = 0
        while index < len(self.hash_list) and s_index < len(delta_list):
            delta = delta_list[s_index]
            (start, length, hash_value) = self.hash_list[index]
            if hash_value < delta.hash_value:
                index += 1
                #print "[Debug] move to next : %d" % index
                continue

            # compare
            if delta.hash_value == hash_value and delta.ref_id == DeltaItem.REF_XDELTA:
                matching_count += 1
                #print "[Debug] page %ld is matching base %ld" % (s_start, start)
                delta.ref_id = ref_id
                delta.data_len = 8
                delta.data = long(start)
            s_index += 1

        #print "[Debug] matching %d out of %d total pages" % (matching_count, len(delta_list))
        return delta_list


def _recover_modified_list(delta_list, raw_path):
    raw_file = open(raw_path, "rb")
    raw_mmap = mmap.mmap(raw_file.fileno(), 0, prot=mmap.PROT_READ)
    delta_list.sort(key=itemgetter('offset'))
    for index, delta_item in enumerate(delta_list):
        #print "processing %d/%d, ref_id: %d, offset: %ld" % \
        #        (index, len(delta_list), delta_item.ref_id, \
        #        delta_item.offset)
        if delta_item.ref_id == DeltaItem.REF_RAW:
            continue
        elif delta_item.ref_id == DeltaItem.REF_BASE_MEM:
            offset = delta_item.data
            recover_data = raw_mmap[offset:offset+Memory.RAM_PAGE_SIZE]
        elif delta_item.ref_id == DeltaItem.REF_SELF:
            ref_offset = delta_item.data
            index = 0
            while index < len(delta_list):
                #print "self referencing : %ld == %ld" % (delta_list[index].offset, ref_offset)
                if delta_list[index].offset == ref_offset:
                    recover_data = delta_list[index].data
                    break
                index += 1
            if index >= len(delta_list):
                raise MemoryError("Cannot find self reference")
        elif delta_item.ref_id == DeltaItem.REF_XDELTA:
            patch_data = delta_item.data
            base_data = raw_mmap[delta_item.offset:delta_item.offset+Memory.RAM_PAGE_SIZE]
            recover_data = tool.merge_data(base_data, patch_data, len(base_data)*2)
        else:
            raise MemoryError("Cannot recover: invalid referce id %d" % delta_item.ref_id)

        if len(recover_data) != Memory.RAM_PAGE_SIZE:
            msg = "Recovered Size Error: %d, ref_id: %d, %ld, %ld" % \
                    (len(recover_data), delta_item.ref_id, delta_item.data_len, delta_item.data)
            raise MemoryError(msg)
        delta_item.ref_id = DeltaItem.REF_RAW
        delta_item.data = recover_data

    raw_file.close()


def _recover_memory(base_path, delta_list, footer, out_path):
    fout = open(out_path, "w+b")

    #sort delta list using offset
    delta_list.sort(key=itemgetter('offset'))

    '''
    for delta_item in delta_list:
        if len(delta_item.data) != Memory.RAM_PAGE_SIZE:
            raise MemoryError("recovered size is not same as page size")
        
        fout.seek(delta_item.offset)
        fout.write(delta_item.data)
    base_file = open(base_path, "rb")
    ram_end_offset, ram_info = Memory._seek_to_end_of_ram(base_file)
    print "ram_end_offset: %ld" % ram_end_offset
    '''
    base_file = open(base_path, "rb")
    delta_list_index = 0
    ram_end_offset, ram_info = Memory._seek_to_end_of_ram(base_file)
    base_file.seek(0)
    while True:
        offset = base_file.tell()
        if len(delta_list) == delta_list_index:
            break

        base_data = base_file.read(Memory.RAM_PAGE_SIZE)
        if len(base_data) != Memory.RAM_PAGE_SIZE:
            raise MemoryError("read base size is not page size")
        
        #import pdb; pdb.set_trace()

        if offset != delta_list[delta_list_index].offset:
            #print "write base data: %d" % len(base_data)
            fout.write(base_data)
        else:
            modi_data = delta_list[delta_list_index].data
            #print "write modi data: %d at %ld" % (len(modi_data), delta_list[delta_list_index].offset)
            fout.write(modi_data)
            delta_list_index += 1

    #print "Write rest part of the base after last modified memory page"
    #print "from %ld to %ld " % (base_file.tell(), ram_end_offset)
    rest_data = ram_end_offset - base_file.tell()
    fout.write(base_file.read(rest_data))
    fout.write(footer)


def hashing(filepath):
    # Contstuct KVM Base Memory DS from KVM migrated memory
    # filepath  : input KVM Memory Snapshot file path
    memory = Memory()
    hash_list, footer_data =  memory._load_file(filepath, print_out=sys.stdout)
    memory.hash_list = hash_list
    memory.footer_data = footer_data
    return memory


def process_cmd(argv):
    COMMANDS = ['hashing', 'delta', 'recover']
    USAGE = "Usage: %prog " + "[%s] [option]" % '|'.join(COMMANDS)
    VERSION = '%prog ' + str(1.0)
    DESCRIPTION = "KVM Memory struction interpreste"

    parser = OptionParser(usage=USAGE, version=VERSION, description=DESCRIPTION)
    parser.add_option("-m", "--migrated_file", type="string", dest="mig_file", action='store', \
            help="Migrated file path")
    parser.add_option("-r", "--raw_file", type="string", dest="raw_file", action='store', \
            help="Raw memory path")
    parser.add_option("-s", "--hash_file", type="string", dest="hash_file", action='store', \
            help="Hashsing file path")
    parser.add_option("-d", "--delta", type="string", dest="delta_file", action='store', \
            default="mem_delta", help="path for delta list")
    parser.add_option("-b", "--base", type="string", dest="base_file", action='store', \
            help="path for base memory file")
    settings, args = parser.parse_args()
    if len(args) != 1:
        parser.error("Cannot find command")
    command = args[0]
    if command not in COMMANDS:
        parser.error("Invalid Command: %s, supporing %s" % (command, ' '.join(COMMANDS)))
    return settings, command


def create_memory_overlay(raw_meta, raw_mem, modified_mem, out_delta, print_out=sys.stdout):
    # get memory delta
    # raw_meta: meta data path of raw memory, e.g. hash_list+footer
    # raw_mem: raw memory path
    # modified_mem: modified memory path
    # out_delta: output path of final delta

    # Create Base Memory from meta file
    base = Memory.import_from_metafile(raw_meta, raw_mem)

    # 1.get modified page
    print_out.write("[Debug] 1.get modified page list\n")
    footer_delta, original_delta_list = base.get_modified(modified_mem)
    delta_list = []
    for item in original_delta_list:
        delta_item = DeltaItem(item.offset, item.offset_len,
                hash_value=item.hash_value,
                ref_id=item.ref_id,
                data_len=item.data_len,
                data=item.data)
        delta_list.append(delta_item)

    # 2.find shared with base memory 
    print_out.write("[Debug] 2.get delta from base Memory\n")
    base.get_delta(delta_list, ref_id=DeltaItem.REF_BASE_MEM)

    # 3.find shared within self
    print_out.write("[Debug] 3.get delta from itself\n")
    DeltaList.get_self_delta(delta_list)

    DeltaList.statistics(delta_list, print_out)
    DeltaList.tofile(footer_delta, delta_list, out_delta)


def recover_memory(base_path, delta_path, raw_meta, out_path):
    # Recover modified memory snapshot
    # base_path: base memory snapshot, delta pages will be applied over it
    # delta_path: memory overlay
    # raw_meta: meta(footer/hash list) information of the raw memory
    # out_path: path to recovered modified memory snapshot

    # Create Base Memory from meta file
    base = Memory.import_from_metafile(raw_meta, base_path)
    footer_delta, delta_list = DeltaList.fromfile(delta_path)

    footer = tool.merge_data(base.footer_data, footer_delta, 1024*1024*10)
    #print "footer size: %ld" % len(footer)
    _recover_modified_list(delta_list, base_path)
    _recover_memory(base_path, delta_list, footer, out_path)

    return delta_list, footer


if __name__ == "__main__":
    settings, command = process_cmd(sys.argv)
    if command == "hashing":
        if not settings.base_file:
            sys.stderr.write("Error, Cannot find migrated file. See help\n")
            sys.exit(1)
        infile = settings.base_file
        base = hashing(infile)
        base.export_to_file(infile+EXT_META)

        # Check Integrity
        re_base = Memory.import_from_metafile(infile+".meta", infile)
        if base.footer_data != re_base.footer_data:
            raise MemoryError("footer data is different")
        print "[SUCCESS] meta file information is matched with original"
    elif command == "delta":
        if (not settings.mig_file) or (not settings.base_file):
            sys.stderr.write("Error, Cannot find modified memory file. See help\n")
            sys.exit(1)
        raw_path = settings.base_file
        meta_path = settings.base_file + EXT_META
        modi_mem_path = settings.mig_file
        out_path = settings.mig_file + ".delta"
        create_memory_overlay(meta_path, raw_path, modi_mem_path, out_path, print_out=sys.stdout)

    elif command == "recover":
        if (not settings.base_file) or (not settings.delta_file):
            sys.stderr.write("Error, Cannot find base/delta file. See help\n")
            sys.exit(1)
        base_path = settings.base_file
        delta_path = settings.delta_file
        raw_meta = settings.base_file + EXT_META
        
        out_path = base_path + ".recover"
        delta_list, footer = recover_memory(base_path, delta_path, raw_meta, out_path)

        # varify with original
        if settings.mig_file:
            modi_mem = open(settings.mig_file, "rb")
            for delta_item in delta_list:
                offset = delta_item.offset
                data = delta_item.data
                modi_mem.seek(offset)
                origin_data = modi_mem.read(len(data))
                if data != origin_data:
                    msg = "orignal data is not same at %ld" % offset
                    raise MemoryError(msg)
            modi_mem.seek(os.path.getsize(settings.mig_file)-len(footer))
            modi_footer = modi_mem.read(len(footer))
            if modi_footer != footer:
                msg = "footer is different %ld != %ld" % (len(modi_footer), len(footer))
                raise MemoryError(msg)
            print "Successfully recovered"

