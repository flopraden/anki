# -*- coding: utf-8 -*-
# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import csv
import re

from anki.importing.noteimp import NoteImporter, ForeignNote
from anki.lang import _


class TextImporter(NoteImporter):

    needDelimiter = True
    patterns = ("\t", "|", ",", ";", ":")

    def __init__(self, *args):
        NoteImporter.__init__(self, *args)
        self.lines = None
        self.fileobj = None
        self.delimiter = None
        self.tagsToAdd = []

    def foreignNotes(self):
        self.open()
        # process all lines
        log = []
        notes = []
        lineNum = 0
        ignored = 0
        if self.delimiter:
            reader = csv.reader(self.data, delimiter=self.delimiter, doublequote=True)
        else:
            reader = csv.reader(self.data, self.dialect, doublequote=True)
        try:
            for row in reader:
                if len(row) != self.numFields:
                    if row:
                        log.append(_(
                            "'%(row)s' had %(num1)d fields, "
                            "expected %(num2)d") % {
                            "row": " ".join(row),
                            "num1": len(row),
                            "num2": self.numFields,
                            })
                        ignored += 1
                    continue
                note = self.noteFromFields(row)
                notes.append(note)
        except (csv.Error) as e:
            log.append(_("Aborted: %s") % str(e))
        self.log = log
        self.ignored = ignored
        self.fileobj.close()
        return notes

    def open(self):
        "Same as cacheFile"
        # load & look for the right pattern
        self.cacheFile()

    def cacheFile(self):
        """Read file into self.lines if not already there.
        set openFile for the remaining.
        """
        if not self.fileobj:
            self.openFile()

    def openFile(self):
        """Put:
        in data: all lines not starting with #, and not tags
        in tags: the tags, separated by space, assuming first line which is not a comment start with "tags:".
        Set CSV reader, or delimiter if csv can't be guessed.
        set numFields to the number of fields of the first non empty line
        set mapping to initial mapping.
        """
        self.dialect = None
        self.fileobj = open(self.file, "r", encoding='utf-8-sig')
        self.data = self.fileobj.read()
        def sub(s):
            return re.sub(r"^\#.*$", "__comment", s)
        #set of lines not starting with #
        self.data = [sub(x)+"\n" for x in self.data.split("\n") if sub(x) != "__comment"]
        if self.data:
            if self.data[0].startswith("tags:"):
                tags = str(self.data[0][5:]).strip()
                self.tagsToAdd = tags.split(" ")
                del self.data[0]
            self.updateDelimiter()
        if not self.dialect and not self.delimiter:
            raise Exception("unknownFormat")

    def updateDelimiter(self):
        """If possible, set CSV dialect, as guessed by CSV library.
        Otherwise, set delimiter to "\t", ";", "," if they are in the first line. To " " otherwise
        set numFields to the number of fields of the first non empty line
        set mapping to initial mapping.
        """
        def err():
            raise Exception("unknownFormat")
        self.dialect = None
        sniffer = csv.Sniffer()
        if not self.delimiter:
            try:
                self.dialect = sniffer.sniff("\n".join(self.data[:10]),
                                             self.patterns)
            except:
                try:
                    self.dialect = sniffer.sniff(self.data[0], self.patterns)
                except:
                    pass
        if self.dialect:
            try:
                reader = csv.reader(self.data, self.dialect, doublequote=True)
            except:
                err()
        else:
            if not self.delimiter:
                if "\t" in self.data[0]:
                    self.delimiter = "\t"
                elif ";" in self.data[0]:
                    self.delimiter = ";"
                elif "," in self.data[0]:
                    self.delimiter = ","
                else:
                    self.delimiter = " "
            reader = csv.reader(self.data, delimiter=self.delimiter, doublequote=True)
        try:
            while True:
                row = next(reader)
                if row:
                    self.numFields = len(row)
                    break
        except:
            err()
        self.initMapping()

    def fields(self):
        "Number of fields."
        self.open()
        return self.numFields

    def noteFromFields(self, fields):
        note = ForeignNote()
        note.fields.extend([x for x in fields])
        note.tags.extend(self.tagsToAdd)
        return note
