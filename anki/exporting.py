# -*- coding: utf-8 -*-
# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import re, os, zipfile, shutil, unicodedata
import json

from anki.lang import _
from anki.utils import ids2str, splitFields, namedtmp, stripHTML
from anki.hooks import runHook
from anki import Collection
from anki.cards import siblings

class Exporter:
    """An abstract class. Inherited by class actually doing some kind of export.

    count -- the number of cards to export.
    """
    includeHTML = None

    def __init__(self, col, did=None):
        #Currently, did is never set during initialisation.
        self.col = col
        self.did = did
        self.cids = None

    def doExport(self, path):
        raise Exception("not implemented")

    def exportInto(self, path):
        """Export into path.

        This is the method called from the GUI to actually export things.

        Keyword arguments:
        path -- a path of file in which to export"""
        self._escapeCount = 0# not used ANYWHERE in the code as of 25 november 2018
        file = open(path, "wb")
        self.doExport(file)
        file.close()

    def processText(self, text):
        if self.includeHTML is False:
            text = self.stripHTML(text)

        text = self.escapeText(text)

        return text

    def escapeText(self, text):
        "Escape newlines, tabs, CSS and quotechar."
        # fixme: we should probably quote fields with newlines
        # instead of converting them to spaces
        text = text.replace("\n", " ")
        text = text.replace("\t", " " * 8)
        text = re.sub("(?i)<style>.*?</style>", "", text)
        text = re.sub(r"\[\[type:[^]]+\]\]", "", text)
        if "\"" in text:
            text = "\"" + text.replace("\"", "\"\"") + "\""
        return text

    def stripHTML(self, text):
        # very basic conversion to text
        s = text
        s = re.sub(r"(?i)<(br ?/?|div|p)>", " ", s)
        s = re.sub(r"\[sound:[^]]+\]", "", s)
        s = stripHTML(s)
        s = re.sub(r"[ \n\t]+", " ", s)
        s = s.strip()
        return s

    def cardIds(self):
        """card ids of cards in deck self.did if it is set, all ids otherwise."""
        if self.cids is not None:
            cids= self.cids
        elif not self.did:
            cids = self.col.db.list("select id from cards")
        else:
            cids = self.col.decks.cids(self.did, children=True)
        self.count = len(cids)
        if self.col.conf.get("exportSiblings", False):
            cids = siblings(cids)
        return cids


# Cards as TSV
######################################################################

class TextCardExporter(Exporter):

    key = _("Cards in Plain Text")
    ext = ".txt"
    includeHTML = True

    def __init__(self, col):
        Exporter.__init__(self, col)

    def doExport(self, file):
        ids = sorted(self.cardIds())
        strids = ids2str(ids)
        def esc(s):
            # strip off the repeated question in answer if exists
            s = re.sub("(?si)^.*<hr id=answer>\n*", "", s)
            return self.processText(s)
        out = ""
        for cid in ids:
            c = self.col.getCard(cid)
            out += esc(c.q())
            out += "\t" + esc(c.a()) + "\n"
        file.write(out.encode("utf-8"))

# Notes as TSV
######################################################################

class TextNoteExporter(Exporter):

    key = _("Notes in Plain Text")
    ext = ".txt"
    includeTags = True
    includeHTML = True

    def __init__(self, col):
        Exporter.__init__(self, col)
        self.includeID = False

    def doExport(self, file):
        cardIds = self.cardIds()
        data = []
        for id, flds, tags in self.col.db.execute("""
select guid, flds, tags from notes
where id in
(select nid from cards
where cards.id in %s)""" % ids2str(cardIds)):
            row = []
            # note id
            if self.includeID:
                row.append(str(id))
            # fields
            row.extend([self.processText(f) for f in splitFields(flds)])
            # tags
            if self.includeTags:
                row.append(tags.strip())
            data.append("\t".join(row))
        self.count = len(data)
        out = "\n".join(data)
        file.write(out.encode("utf-8"))

# Anki decks
######################################################################
# media files are stored in self.mediaFiles, but not exported.

class AnkiExporter(Exporter):

    key = _("Anki 2.0 Deck")
    ext = ".anki2"
    includeSched = False
    includeMedia = True

    def __init__(self, col):
        Exporter.__init__(self, col)

    def exportInto(self, path):
        # sched info+v2 scheduler not compatible w/ older clients
        self._v2sched = self.col.schedVer() != 1 and self.includeSched

        # create a new collection at the target
        try:
            os.unlink(path)
        except (IOError, OSError):
            pass
        self.dst = Collection(path)
        self.src = self.col
        # find cards
        cids = self.cardIds()
        # copy cards, noting used nids
        nids = {}
        data = []
        for row in self.src.db.execute(
            "select * from cards where id in "+ids2str(cids)):
            nids[row[1]] = True
            data.append(row)
            # clear flags
            row = list(row)
            row[-2] = 0
        self.dst.db.executemany(
            "insert into cards values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            data)
        # notes
        strnids = ids2str(list(nids.keys()))
        notedata = []
        for row in self.src.db.all(
            "select * from notes where id in "+strnids):
            # remove system tags if not exporting scheduling info
            if not self.includeSched:
                row = list(row)
                row[5] = self.removeSystemTags(row[5])
            notedata.append(row)
        self.dst.db.executemany(
            "insert into notes values (?,?,?,?,?,?,?,?,?,?,?)",
            notedata)
        # models used by the notes
        mids = self.dst.db.list("select distinct mid from notes where id in "+
                                strnids)
        # card history and revlog
        if self.includeSched:
            data = self.src.db.all(
                "select * from revlog where cid in "+ids2str(cids))
            self.dst.db.executemany(
                "insert into revlog values (?,?,?,?,?,?,?,?,?)",
                data)
        else:
            # need to reset card state
            self.dst.sched.resetCards(cids)
        # models - start with zero
        self.dst.models.models = {}
        for m in self.src.models.all():
            if int(m['id']) in mids:
                self.dst.models.update(m)
        # decks
        if not self.did:
            dids = []
        else:
            dids = [self.did] + [
                x[1] for x in self.src.decks.children(self.did)]
        dconfs = {}
        for d in self.src.decks.all():
            if str(d['id']) == "1":
                continue
            if dids and d['id'] not in dids:
                continue
            if not d['dyn'] and d['conf'] != 1:
                if self.includeSched:
                    dconfs[d['conf']] = True
            if not self.includeSched:
                # scheduling not included, so reset deck settings to default
                d = dict(d)
                d['conf'] = 1
            self.dst.decks.update(d)
        # copy used deck confs
        for dc in self.src.decks.allConf():
            if dc['id'] in dconfs:
                self.dst.decks.updateConf(dc)
        # find used media
        media = {}
        self.mediaDir = self.src.media.dir()
        if self.includeMedia:
            for row in notedata:
                flds = row[6]
                mid = row[2]
                for file in self.src.media.filesInStr(mid, flds):
                    # skip files in subdirs
                    if file != os.path.basename(file):
                        continue
                    media[file] = True
            if self.mediaDir:
                for fname in os.listdir(self.mediaDir):
                    path = os.path.join(self.mediaDir, fname)
                    if os.path.isdir(path):
                        continue
                    if fname.startswith("_"):
                        # Scan all models in mids for reference to fname
                        for m in self.src.models.all():
                            if int(m['id']) in mids:
                                if self._modelHasMedia(m, fname):
                                    media[fname] = True
                                    break
        self.mediaFiles = list(media.keys())
        self.dst.crt = self.src.crt
        # todo: tags?
        self.count = self.dst.cardCount()
        self.dst.setMod()
        self.postExport()
        self.dst.close()

    def postExport(self):
        # overwrite to apply customizations to the deck before it's closed,
        # such as update the deck description
        pass

    def removeSystemTags(self, tags):
        return self.src.tags.remFromStr("marked leech", tags)

    def _modelHasMedia(self, model, fname):
        # First check the styling
        if fname in model["css"]:
            return True
        # If no reference to fname then check the templates as well
        for t in model["tmpls"]:
            if fname in t["qfmt"] or fname in t["afmt"]:
                return True
        return False

# Packaged Anki decks
######################################################################

class AnkiPackageExporter(AnkiExporter):

    key = _("Anki Deck Package")
    ext = ".apkg"

    def __init__(self, col):
        AnkiExporter.__init__(self, col)

    def exportInto(self, path):
        # open a zip file
        z = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, allowZip64=True)
        media = self.doExport(z, path)
        # media map
        z.writestr("media", json.dumps(media))
        z.close()

    def doExport(self, z, path):
        # export into the anki2 file
        colfile = path.replace(".apkg", ".anki2")
        AnkiExporter.exportInto(self, colfile)
        if not self._v2sched:
            z.write(colfile, "collection.anki2")
        else:
            # fixme: remove in the future
            raise Exception("Please switch to the normal scheduler before exporting a single deck with scheduling information.")

            # prevent older clients from accessing
            # pylint: disable=unreachable
            self._addDummyCollection(z)
            z.write(colfile, "collection.anki21")

        # and media
        self.prepareMedia()
        media = self._exportMedia(z, self.mediaFiles, self.mediaDir)
        # tidy up intermediate files
        os.unlink(colfile)
        p = path.replace(".apkg", ".media.db2")
        if os.path.exists(p):
            os.unlink(p)
        os.chdir(self.mediaDir)
        shutil.rmtree(path.replace(".apkg", ".media"))
        return media

    def _exportMedia(self, z, files, fdir):
        media = {}
        for c, file in enumerate(files):
            cStr = str(c)
            mpath = os.path.join(fdir, file)
            if os.path.isdir(mpath):
                continue
            if os.path.exists(mpath):
                if re.search(r'\.svg$', file, re.IGNORECASE):
                    z.write(mpath, cStr, zipfile.ZIP_DEFLATED)
                else:
                    z.write(mpath, cStr, zipfile.ZIP_STORED)
                media[cStr] = unicodedata.normalize("NFC", file)
                runHook("exportedMediaFiles", c)

        return media

    def prepareMedia(self):
        # chance to move each file in self.mediaFiles into place before media
        # is zipped up
        pass

    # create a dummy collection to ensure older clients don't try to read
    # data they don't understand
    def _addDummyCollection(self, zip):
        path = namedtmp("dummy.anki2")
        c = Collection(path)
        n = c.newNote()
        n[_('Front')] = "This file requires a newer version of Anki."
        c.addNote(n)
        c.save()
        c.close()

        zip.write(path, "collection.anki2")
        os.unlink(path)

# Collection package
######################################################################

class AnkiCollectionPackageExporter(AnkiPackageExporter):

    key = _("Anki Collection Package")
    ext = ".colpkg"
    verbatim = True
    includeSched = None

    def __init__(self, col):
        AnkiPackageExporter.__init__(self, col)

    def doExport(self, z, path):
        # close our deck & write it into the zip file, and reopen
        self.count = self.col.cardCount()
        v2 = self.col.schedVer() != 1
        self.col.close()
        if not v2:
            z.write(self.col.path, "collection.anki2")
        else:
            self._addDummyCollection(z)
            z.write(self.col.path, "collection.anki21")
        self.col.reopen()
        # copy all media
        if not self.includeMedia:
            return {}
        mdir = self.col.media.dir()
        return self._exportMedia(z, os.listdir(mdir), mdir)

# Export modules
##########################################################################

def exporters():
    """A list of pairs (description of an exporter class, the class)"""
    def id(obj):
        return ("%s (*%s)" % (obj.key, obj.ext), obj)
    exps = [
        id(AnkiCollectionPackageExporter),
        id(AnkiPackageExporter),
        id(TextNoteExporter),
        id(TextCardExporter),
    ]
    runHook("exportersList", exps)
    return exps
