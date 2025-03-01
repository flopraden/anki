# Copyright: Ankitects Pty Ltd and contributors
# -*- coding: utf-8 -*-
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""Module for managing add-ons.

An add-on here is defined as a subfolder in the add-on folder containing a file __init__.py
A managed add-on is an add-on whose folder's name contains only
digits.

dir -- the name of the subdirectory of the add-on in the add-on manager
"""

import io
import json
import re
import zipfile
from collections import defaultdict
import markdown
from send2trash import send2trash
import jsonschema
from jsonschema.exceptions import ValidationError

from aqt.qt import *
from aqt.utils import showInfo, openFolder, isWin, openLink, \
    askUser, restoreGeom, saveGeom, restoreSplitter, saveSplitter, \
    showWarning, tooltip, getFile
from zipfile import ZipFile
import aqt.forms
import aqt
from aqt.downloader import download
from anki.lang import _, ngettext
from anki.utils import intTime
from anki.sync import AnkiRequestsClient

class AddonManager:
    """
    dirty -- whether an add-on is loaded
    mw -- the main window
    """

    ext = ".ankiaddon"
    _manifest_schema = {
        "type": "object",
        "properties": {
            "package": {"type": "string", "meta": False},
            "name": {"type": "string", "meta": True},
            "mod": {"type": "number", "meta": True},
            "conflicts": {
                "type": "array",
                "items": {"type": "string"},
                "meta": True
            }
        },
        "required": ["package", "name"]
    }

    def __init__(self, mw):
        self.mw = mw
        self.dirty = False
        f = self.mw.form
        f.actionAdd_ons.triggered.connect(self.onAddonsDialog)
        sys.path.insert(0, self.addonsFolder())

    def allAddons(self):
        """List of installed add-ons' folder name

        In alphabetical order of folder name. I.e. add-on number for downloaded add-ons.
        Reverse order if the environment variable  ANKIREVADDONS is set.

        A folder is an add-on folder iff it contains __init__.py.

        """
        l = []
        for d in os.listdir(self.addonsFolder()):
            path = self.addonsFolder(d)
            if not os.path.exists(os.path.join(path, "__init__.py")):
                continue
            l.append(d)
        l.sort()
        if os.getenv("ANKIREVADDONS", ""):
            l = reversed(l)
        return l

    def managedAddons(self):
        """List of managed add-ons.

        In alphabetical order of folder name. I.e. add-on number for downloaded add-ons.
        Reverse order if the environment variable  ANKIREVADDONS is set.
        """
        return [d for d in self.allAddons()
                if re.match(r"^\d+$", d)]

    def addonsFolder(self, dir=None):
        """Path to a folder.

        To the add-on folder by default, guaranteed to exists.
        If dir is set, then the path to the add-on dir, not guaranteed
        to exists

        dir -- TODO
        """

        root = self.mw.pm.addonFolder()
        if not dir:
            return root
        return os.path.join(root, dir)

    def loadAddons(self):
        for dir in self.allAddons():
            if dir in incorporatedAddonsDict or (re.match(r"^\d+$", dir) and int(dir) in incorporatedAddonsDict):
                continue
            meta = self.addonMeta(dir)
            if meta.get("disabled"):
                continue
            self.dirty = True
            try:
                __import__(dir)
            except:
                showWarning(_("""\
An add-on you installed failed to load. If problems persist, please \
go to the Tools>Add-ons menu, and disable or delete the add-on.

When loading '%(name)s':
%(traceback)s
""") % dict(name=meta.get("name", dir), traceback=traceback.format_exc()))

    def onAddonsDialog(self):
        AddonsDialog(self)

    # Metadata
    ######################################################################

    def _addonMetaPath(self, dir):
        """Path of the configuration of the addon dir"""
        return os.path.join(self.addonsFolder(dir), "meta.json")

    def addonMeta(self, dir):
        path = self._addonMetaPath(dir)
        try:
            with open(path, encoding="utf8") as f:
                return json.load(f)
        except:
            return dict()

    def writeAddonMeta(self, dir, meta):
        path = self._addonMetaPath(dir)
        with open(path, "w", encoding="utf8") as f:
            json.dump(meta, f)

    def isEnabled(self, dir):
        meta = self.addonMeta(dir)
        return not meta.get('disabled')

    def toggleEnabled(self, dir, enable=None):
        meta = self.addonMeta(dir)
        enabled = enable if enable is not None else meta.get("disabled")
        if enabled is True:
            conflicting = self._disableConflicting(dir)
            if conflicting:
                addons = ", ".join(self.addonName(f) for f in conflicting)
                showInfo(
                    _("The following add-ons are incompatible with %(name)s \
and have been disabled: %(found)s") % dict(name=self.addonName(dir), found=addons),
                    textFormat="plain")

        meta['disabled'] = not enabled
        self.writeAddonMeta(dir, meta)

    def addonName(self, dir):
        """The name of the addon.

        It is found either in "name" parameter of the configuration in
        directory dir of the add-on directory.
        Otherwise dir is used."""
        return self.addonMeta(dir).get("name", dir)

    def annotatedName(self, dir):
        buf = self.addonName(dir)
        if not self.isEnabled(dir):
            buf += _(" (disabled)")
        return buf

    # Conflict resolution
    ######################################################################

    def addonConflicts(self, dir):
        return self.addonMeta(dir).get("conflicts", [])

    def allAddonConflicts(self):
        all_conflicts = defaultdict(list)
        for dir in self.allAddons():
            if not self.isEnabled(dir):
                continue
            conflicts = self.addonConflicts(dir)
            for other_dir in conflicts:
                all_conflicts[other_dir].append(dir)
        return all_conflicts

    def _disableConflicting(self, dir, conflicts=None):
        conflicts = conflicts or self.addonConflicts(dir)

        installed = self.allAddons()
        found = [d for d in conflicts if d in installed and self.isEnabled(d)]
        found.extend(self.allAddonConflicts().get(dir, []))
        if not found:
            return []

        for package in found:
            self.toggleEnabled(package, enable=False)

        return found

    # Installing and deleting add-ons
    ######################################################################

    def readManifestFile(self, zfile):
        try:
            with zfile.open("manifest.json") as f:
                data = json.loads(f.read())
            jsonschema.validate(data, self._manifest_schema)
            # build new manifest from recognized keys
            schema = self._manifest_schema["properties"]
            manifest = {key: data[key] for key in data.keys() & schema.keys()}
        except (KeyError, json.decoder.JSONDecodeError, ValidationError):
            # raised for missing manifest, invalid json, missing/invalid keys
            return {}
        return manifest

    def install(self, file, manifest=None):
        """Install add-on from path or file-like object. Metadata is read
        from the manifest file, with keys overriden by supplying a 'manifest'
        dictionary"""
        try:
            zfile = ZipFile(file)
        except zipfile.BadZipfile:
            return False, "zip"

        with zfile:
            file_manifest = self.readManifestFile(zfile)
            if manifest:
                file_manifest.update(manifest)
            manifest = file_manifest
            if not manifest:
                return False, "manifest"
            package = manifest["package"]
            conflicts = manifest.get("conflicts", [])
            found_conflicts = self._disableConflicting(package,
                                                       conflicts)
            meta = self.addonMeta(package)
            self._install(package, zfile)
        schema = self._manifest_schema["properties"]
        manifest_meta = {k: v for k, v in manifest.items()
                         if k in schema and schema[k]["meta"]}
        meta.update(manifest_meta)
        self.writeAddonMeta(package, meta)

        return True, meta["name"], found_conflicts

    def _install(self, dir, zfile):
        # previously installed?
        base = self.addonsFolder(dir)
        if os.path.exists(base):
            self.backupUserFiles(dir)
            if not self.deleteAddon(dir): # To install, previous version should be deleted. If it can't be deleted for an unkwown reason, we try to put everything back in previous state.
                self.restoreUserFiles(dir)
                return

        os.mkdir(base)
        self.restoreUserFiles(dir)

        # extract
        for n in zfile.namelist():
            if n.endswith("/"):
                # folder; ignore
                continue

            path = os.path.join(base, n)
            # skip existing user files
            if os.path.exists(path) and n.startswith("user_files/"):
                continue
            zfile.extract(n, base)

    def deleteAddon(self, dir):
        """Delete the add-on folder of add-on dir. Returns True on success"""
        try:
            send2trash(self.addonsFolder(dir))
            return True
        except OSError as e:
            showWarning(_("Unable to update or delete add-on. Please start Anki while holding down the shift key to temporarily disable add-ons, then try again.\n\nDebug info: %s") % e,
                        textFormat="plain")
            return False

    # Processing local add-on files
    ######################################################################

    def processPackages(self, paths):
        log = []
        errs = []
        self.mw.progress.start(immediate=True)
        try:
            for path in paths:
                base = os.path.basename(path)
                ret = self.install(path)
                if ret[0] is False:
                    if ret[1] == "zip":
                        msg = _("Corrupt add-on file.")
                    elif ret[1] == "manifest":
                        msg = _("Invalid add-on manifest.")
                    else:
                        msg = "Unknown error: {}".format(ret[1])
                    errs.append(_("Error installing <i>%(base)s</i>: %(error)s"
                                  % dict(base=base, error=msg)))
                else:
                    log.append(_("Installed %(name)s" % dict(name=ret[1])))
                    if ret[2]:
                        log.append(_("The following conflicting add-ons were disabled:") + " " + " ".join(ret[2]))
        finally:
            self.mw.progress.finish()
        return log, errs

    # Downloading
    ######################################################################

    def downloadIds(self, ids):
        log = []
        errs = []
        self.mw.progress.start(immediate=True)
        for n in ids:
            ret = download(self.mw, n)
            if ret[0] == "error":
                errs.append(_("Error downloading %(id)s: %(error)s") % dict(id=n, error=ret[1]))
                continue
            data, fname = ret
            fname = fname.replace("_", " ")
            name = os.path.splitext(fname)[0]
            ret = self.install(io.BytesIO(data),
                               manifest={"package": str(n), "name": name,
                                         "mod": intTime()})
            if ret[0] is False:
                if ret[1] == "zip":
                    msg = _("Corrupt add-on file.")
                elif ret[1] == "manifest":
                    msg = _("Invalid add-on manifest.")
                else:
                    msg = "Unknown error: {}".format(ret[1])
                errs.append(_("Error downloading %(id)s: %(error)s") % dict(
                    id=n, error=msg))
            else:
                log.append(_("Downloaded %(fname)s" % dict(fname=name)))
                if ret[2]:
                    log.append(_("The following conflicting add-ons were disabled:") + " " + " ".join(ret[2]))

        self.mw.progress.finish()
        return log, errs

    # Updating
    ######################################################################

    def checkForUpdates(self):
        """The list of add-ons not up to date. Compared to the server's information."""
        client = AnkiRequestsClient()

        # get mod times
        self.mw.progress.start(immediate=True)
        try:
            # ..of enabled items downloaded from ankiweb
            addons = []
            for dir in self.managedAddons():
                meta = self.addonMeta(dir)
                if not meta.get("disabled"):
                    addons.append(dir)

            mods = []
            while addons:
                chunk = addons[:25]
                del addons[:25]
                mods.extend(self._getModTimes(client, chunk))
            return self._updatedIds(mods)
        finally:
            self.mw.progress.finish()

    def _getModTimes(self, client, chunk):
        """The list of (id,mod time) for add-ons whose id is in chunk.

        client -- an ankiRequestsclient
        chunck -- a list of add-on number"""
        resp = client.get(
            aqt.appShared + "updates/" + ",".join(chunk))
        if resp.status_code == 200:
            return resp.json()
        else:
            raise Exception("Unexpected response code from AnkiWeb: {}".format(resp.status_code))

    def _updatedIds(self, mods):
        """Given a list of (id,last mod on server), returns the sublist of
        add-ons not up to date."""
        updated = []
        for dir, ts in mods:
            sid = str(dir)
            if self.addonMeta(sid).get("mod", 0) < (ts or 0):
                updated.append(sid)
        return updated

    # Add-on Config
    ######################################################################

    """Dictionnary from modules to function to apply when add-on
    manager is called on this config."""
    _configButtonActions = {}
    """Dictionnary from modules to function to apply when add-on
    manager ends an update. Those functions takes the configuration,
    parsed as json, in argument."""
    _configUpdatedActions = {}

    def addonConfigDefaults(self, dir):
        """The (default) configuration of the addon whose
        name/directory is dir.

        This file should be called config.json"""
        path = os.path.join(self.addonsFolder(dir), "config.json")
        try:
            with open(path, encoding="utf8") as f:
                return json.load(f)
        except:
            return None

    def addonConfigHelp(self, dir):
        """The configuration of this addon, obtained as configuration"""
        path = os.path.join(self.addonsFolder(dir), "config.md")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return markdown.markdown(f.read())
        else:
            return ""

    def addonFromModule(self, module):
        """Returns the string of module before the first dot"""
        return module.split(".")[0]

    def configAction(self, addon):
        """The function to call for addon when add-on manager ask for
        edition of its configuration."""
        return self._configButtonActions.get(addon)

    def configUpdatedAction(self, addon):
        """The function to call for addon when add-on edition has been done
        using add-on manager.

        """
        return self._configUpdatedActions.get(addon)

    # Add-on Config API
    ######################################################################

    def getConfig(self, module):
        """The current configuration.

        More precisely:
        -None if the module has no file config.json
        -otherwise the union of:
        --default config from config.json
        --the last version of the config, as saved in meta

        Note that if you edited the dictionary obtained from the
        configuration file without calling self.writeConfig(module,
        config), then getConfig will not return current config

        """
        addon = self.addonFromModule(module)
        # get default config
        config = self.addonConfigDefaults(addon)
        if config is None:
            return None
        # merge in user's keys
        meta = self.addonMeta(addon)
        userConf = meta.get("config", {})
        config.update(userConf)
        return config

    def setConfigAction(self, module, fn):
        """Change the action of add-on manager for the edition of the
        current add-ons config.

        Each time the user click in the add-on manager on the button
        "config" button, fn is called. Unless fn is falsy, in which
        case the standard procedure is used

        Keyword arguments:
        module -- the module/addon considered
        fn -- a function taking no argument, or a falsy value
        """
        addon = self.addonFromModule(module)
        self._configButtonActions[addon] = fn

    def setConfigUpdatedAction(self, module, fn):
        """Allow a function to add on new configurations.

        Each time the configuration of module is modified in the
        add-on manager, fn is called on the new configuration.

        Keyword arguments:
        module -- __name__ from module's code
        fn -- A function taking the configuration, parsed as json, in
        """
        addon = self.addonFromModule(module)
        self._configUpdatedActions[addon] = fn

    def writeConfig(self, module, conf):
        """The config for the module whose name is module  is now conf"""
        addon = self.addonFromModule(module)
        meta = self.addonMeta(addon)
        meta['config'] = conf
        self.writeAddonMeta(addon, meta)

    # user_files
    ######################################################################

    def _userFilesPath(self, sid):
        """The path of the user file's folder."""
        return os.path.join(self.addonsFolder(sid), "user_files")

    def _userFilesBackupPath(self):
        """A path to use for back-up. It's independent of the add-on number."""
        return os.path.join(self.addonsFolder(), "files_backup")

    def backupUserFiles(self, sid):
        """Move user file's folder to a folder called files_backup in the add-on folder"""
        p = self._userFilesPath(sid)
        if os.path.exists(p):
            os.rename(p, self._userFilesBackupPath())

    def restoreUserFiles(self, sid):
        """Move the back up of user file's folder to its normal location in
        the folder of the addon sid"""
        p = self._userFilesPath(sid)
        bp = self._userFilesBackupPath()
        # did we back up userFiles?
        if not os.path.exists(bp):
            return
        os.rename(bp, p)

    # Web Exports
    ######################################################################

    _webExports = {}

    def setWebExports(self, module, pattern):
        addon = self.addonFromModule(module)
        self._webExports[addon] = pattern

    def getWebExports(self, addon):
        return self._webExports.get(addon)


# Add-ons Dialog
######################################################################

class AddonsDialog(QDialog):

    def __init__(self, addonsManager):
        self.mgr = addonsManager
        self.mw = addonsManager.mw

        super().__init__(self.mw)

        f = self.form = aqt.forms.addons.Ui_Dialog()
        f.setupUi(self)
        f.getAddons.clicked.connect(self.onGetAddons)
        f.installFromFile.clicked.connect(self.onInstallFiles)
        f.checkForUpdates.clicked.connect(self.onCheckForUpdates)
        f.toggleEnabled.clicked.connect(self.onToggleEnabled)
        f.viewPage.clicked.connect(self.onViewPage)
        f.viewFiles.clicked.connect(self.onViewFiles)
        f.delete_2.clicked.connect(self.onDelete)
        f.config.clicked.connect(self.onConfig)
        self.form.addonList.itemDoubleClicked.connect(self.onConfig)
        self.form.addonList.currentRowChanged.connect(self._onAddonItemSelected)
        self.setAcceptDrops(True)
        self.redrawAddons()
        restoreGeom(self, "addons")
        self.show()

    def dragEnterEvent(self, event):
        mime = event.mimeData()
        if not mime.hasUrls():
            return None
        urls = mime.urls()
        ext = self.mgr.ext
        if all(url.toLocalFile().endswith(ext) for url in urls):
            event.acceptProposedAction()

    def dropEvent(self, event):
        mime = event.mimeData()
        paths = []
        for url in mime.urls():
            path = url.toLocalFile()
            if os.path.exists(path):
                paths.append(path)
        self.onInstallFiles(paths)

    def reject(self):
        saveGeom(self, "addons")
        return QDialog.reject(self)

    def redrawAddons(self):
        addonList = self.form.addonList
        mgr = self.mgr

        self.addons = [(mgr.annotatedName(d), d) for d in mgr.allAddons()]
        self.addons.sort()

        selected = set(self.selectedAddons())
        addonList.clear()
        for name, dir in self.addons:
            item = QListWidgetItem(name, addonList)
            if not mgr.isEnabled(dir):
                item.setForeground(Qt.gray)
            if dir in selected:
                item.setSelected(True)

        addonList.repaint()

    def _onAddonItemSelected(self, row_int):
        try:
            addon = self.addons[row_int][1]
        except IndexError:
            addon = ''
        self.form.viewPage.setEnabled(bool(re.match(r"^\d+$", addon)))
        self.form.config.setEnabled(bool(self.mgr.getConfig(addon) or
                                         self.mgr.configAction(addon)))

    def selectedAddons(self):
        idxs = [x.row() for x in self.form.addonList.selectedIndexes()]
        return [self.addons[idx][1] for idx in idxs]

    def onlyOneSelected(self):
        dirs = self.selectedAddons()
        if len(dirs) != 1:
            showInfo(_("Please select a single add-on first."))
            return
        return dirs[0]

    def onToggleEnabled(self):
        for dir in self.selectedAddons():
            self.mgr.toggleEnabled(dir)
        self.redrawAddons()

    def onViewPage(self):
        addon = self.onlyOneSelected()
        if not addon:
            return
        if re.match(r"^\d+$", addon):
            openLink(aqt.appShared + "info/{}".format(addon))
        else:
            showWarning(_("Add-on was not downloaded from AnkiWeb."))

    def onViewFiles(self):
        # if nothing selected, open top level folder
        selected = self.selectedAddons()
        if not selected:
            openFolder(self.mgr.addonsFolder())
            return

        # otherwise require a single selection
        addon = self.onlyOneSelected()
        if not addon:
            return
        path = self.mgr.addonsFolder(addon)
        openFolder(path)

    def onDelete(self):
        selected = self.selectedAddons()
        if not selected:
            return
        if not askUser(ngettext("Delete the %(num)d selected add-on?",
                                "Delete the %(num)d selected add-ons?",
                                len(selected)) %
                               dict(num=len(selected))):
            return
        for dir in selected:
            if not self.mgr.deleteAddon(dir):
                break
        self.form.addonList.clearSelection()
        self.redrawAddons()

    def onGetAddons(self):
        GetAddons(self)

    def onInstallFiles(self, paths=None):
        if not paths:
            key = (_("Packaged Anki Add-on") + " (*{})".format(self.mgr.ext))
            paths = getFile(self, _("Install Add-on(s)"), None, key,
                            key="addons", multi=True)
            if not paths:
                return False

        log, errs = self.mgr.processPackages(paths)

        if log:
            log_html = "<br>".join(log)
            if len(log) == 1:
                tooltip(log_html, parent=self)
            else:
                showInfo(log_html, parent=self, textFormat="rich")
        if errs:
            msg = _("Please report this to the respective add-on author(s).")
            showWarning("<br><br>".join(errs + [msg]), parent=self, textFormat="rich")

        self.redrawAddons()

    def onCheckForUpdates(self):
        try:
            updated = self.mgr.checkForUpdates()
        except Exception as e:
            showWarning(_("Please check your internet connection.") + "\n\n" + str(e),
                        textFormat="plain")
            return

        if not updated:
            tooltip(_("No updates available."))
        else:
            names = [self.mgr.addonName(d) for d in updated]
            if askUser(_("Update the following add-ons?") +
                               "\n" + "\n".join(names)):
                log, errs = self.mgr.downloadIds(updated)
                if log:
                    log_html = "<br>".join(log)
                    if len(log) == 1:
                        tooltip(log_html, parent=self)
                    else:
                        showInfo(log_html, parent=self, textFormat="rich")
                if errs:
                    showWarning("\n\n".join(errs), parent=self, textFormat="plain")

                self.redrawAddons()

    def onConfig(self):
        """Assuming a single addon is selected, either:
        -if this add-on as a special config, set using setConfigAction, with a
        truthy value, call this config.
        -otherwise, call the config editor on the current config of
        this add-on"""

        addon = self.onlyOneSelected()
        if not addon:
            return

        # does add-on manage its own config?
        act = self.mgr.configAction(addon)
        if act:
            act()
            return

        conf = self.mgr.getConfig(addon)
        if conf is None:
            showInfo(_("Add-on has no configuration."))
            return

        ConfigEditor(self, addon, conf)


# Fetching Add-ons
######################################################################

class GetAddons(QDialog):

    def __init__(self, dlg):
        QDialog.__init__(self, dlg)
        self.addonsDlg = dlg
        self.mgr = dlg.mgr
        self.mw = self.mgr.mw
        self.form = aqt.forms.getaddons.Ui_Dialog()
        self.form.setupUi(self)
        b = self.form.buttonBox.addButton(
            _("Browse Add-ons"), QDialogButtonBox.ActionRole)
        b.clicked.connect(self.onBrowse)
        restoreGeom(self, "getaddons", adjustSize=True)
        self.exec_()
        saveGeom(self, "getaddons")

    def onBrowse(self):
        openLink(aqt.appShared + "addons/2.1")

    def accept(self):
        # get codes
        try:
            ids = [int(n) for n in self.form.code.text().split()]
        except ValueError:
            showWarning(_("Invalid code."))
            return

        log, errs = self.mgr.downloadIds(ids)

        if log:
            log_html = "<br>".join(log)
            if len(log) == 1:
                tooltip(log_html, parent=self)
            else:
                showInfo(log_html, parent=self, textFormat="rich")
        if errs:
            showWarning("\n\n".join(errs), textFormat="plain")

        self.addonsDlg.redrawAddons()
        QDialog.accept(self)

# Editing config
######################################################################

def readableJson(text):
    """Text, where \n are replaced with new line. Unless it's preceded by a odd number of \."""
    l=[]
    numberOfSlashOdd=False
    numberOfQuoteOdd=False
    for char in text:
        if char == "n" and numberOfQuoteOdd and numberOfSlashOdd:
            l[-1]="\n"
            debug("replacing last slash by newline")
        else:
            l.append(char)
            if char=="\n":
                char="newline"
            debug(f"adding {char}")

        if char == "\"":
            if not numberOfSlashOdd:
                numberOfQuoteOdd = not numberOfQuoteOdd
                debug(f"numberOfQuoteOdd is now {numberOfQuoteOdd}")

        if char == "\\":
            numberOfSlashOdd = not numberOfSlashOdd
        else:
            numberOfSlashOdd = False
        debug(f"numberOfSlashOdd is now {numberOfSlashOdd}")
    return "".join(l)



class ConfigEditor(QDialog):

    def __init__(self, dlg, addon, conf):
        super().__init__(dlg)
        self.addon = addon
        self.conf = conf
        self.mgr = dlg.mgr
        self.form = aqt.forms.addonconf.Ui_Dialog()
        self.form.setupUi(self)
        restore = self.form.buttonBox.button(QDialogButtonBox.RestoreDefaults)
        restore.clicked.connect(self.onRestoreDefaults)
        self.setupFonts()
        self.updateHelp()
        self.updateText(self.conf)
        restoreGeom(self, "addonconf")
        restoreSplitter(self.form.splitter, "addonconf")
        self.show()

    def onRestoreDefaults(self):
        default_conf = self.mgr.addonConfigDefaults(self.addon)
        self.updateText(default_conf)
        tooltip(_("Restored defaults"), parent=self)

    def setupFonts(self):
        font_mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        font_mono.setPointSize(font_mono.pointSize() + 1)
        self.form.editor.setFont(font_mono)

    def updateHelp(self):
        txt = self.mgr.addonConfigHelp(self.addon)
        if txt:
            self.form.label.setText(txt)
        else:
            self.form.scrollArea.setVisible(False)

    def updateText(self, conf):
        self.form.editor.setPlainText(
            readableJson(json.dumps(conf,sort_keys=True,indent=4, separators=(',', ': '))))

    def onClose(self):
        saveGeom(self, "addonconf")
        saveSplitter(self.form.splitter, "addonconf")

    def reject(self):
        self.onClose()
        super().reject()

    def accept(self):
        """
        Transform the new config into json, and either:
        -pass it to the special config function, set using
        setConfigUpdatedAction if it exists,
        -or save it as configuration otherwise.

        If the config is not proper json, show an error message and do
        nothing.
        -if the special config is falsy, just save the value
        """
        txt = self.form.editor.toPlainText()
        try:
            new_conf = json.loads(txt)
        except Exception as e:
            showInfo(_("Invalid configuration: ") + repr(e))
            return

        if not isinstance(new_conf, dict):
            showInfo(_("Invalid configuration: top level object must be a map"))
            return

        if new_conf != self.conf:
            self.mgr.writeConfig(self.addon, new_conf)
            # does the add-on define an action to be fired?
            act = self.mgr.configUpdatedAction(self.addon)
            if act:
                act(new_conf)

        self.onClose()
        super().accept()

## Add-ons incorporated in this fork.

class Addon:
    def __init__(self, name = None, id = None, mod = None, gitHash = None, gitRepo = None):
        self.name = name
        self.id = id
        self.mod = mod
        self.gitHash = gitHash
        self.gitRepo = gitRepo

    def __hash__(self):
        return self.id or hash(self.name)

""" Set of characteristic of Add-ons incorporated here"""
incorporatedAddonsSet = {
    Addon("Add a tag to notes with missing media", 2027876532, 1560318502, "26c4f6158ce2b8811b8ac600ed8a0204f5934d0b", "Arthur-Milchior/anki-tag-missing-medias"),
    Addon("Adding note and changing note type become quicker", 802285486, gitHash = "f1b2df03f4040e7820454052a2088a7672d819b2", gitRepo = "https://github.com/Arthur-Milchior/anki-fast-note-type-editor"),
    Addon("Advanced note editor Multi-column Frozen fields", 2064123047, 1561905302, "82a27f2726598c25d06f3065d23eb988815efd25", "https://github.com/Arthur-Milchior/anki-Multi-column-edit-window"),
    Addon("Allows empty first field during adding and import", 46741504, 1553438887, "7224199", "https://github.com/Arthur-Milchior/anki-empty-first-field"),
    Addon("Batch Editing", 291119185, 1560116344, "https://github.com/glutanimate/batch-editing", "41149dbec543b019a9eb01d06d2a3c5d13b8d830"),
    Addon("Change cards decks prefix", 1262882834, 1550534152, "f9843693dafb4aeb2248de5faf44cf5b5fdc69ec", "https://github.com/Arthur-Milchior/anki-deck-prefix-edit"),
    Addon("Change a notes type without requesting a database upload", 719871418, 1560313030, "8b97cfbea1b7d8bd7124d6ebe6553f36d1914823", "https://github.com/Arthur-Milchior/anki-change-note-type"),
    Addon("«Check database» Explain errors and what is done to fix it", 1135180054, gitHash = "371c360e5611ad3eec5dcef400d969e7b1572141", gitRepo = "https://github.com/Arthur-Milchior/anki-database-check-explained"), #mod unkwon because it's not directly used by the author anymore
    Addon("Copy notes", 1566928056, 1560116343, "e12e62094211a8bf06d025ee0f325e8aa4489292", "https://github.com/Arthur-Milchior/anki-copy-note"),
    Addon("Correcting a bug in anki which makes new card appearing in wrong order", 127334978, 1561608317, "9ec2f1e5c2f4d95de82b6cc7a43bf68cb39a26f7", "https://github.com/Arthur-Milchior/anki-correct-due"),
    Addon("Empty cards returns more usable informations", 25425599, 1560126141, "299a0a7b3092923f5932da0bf8ec90e16db269af", "https://github.com/Arthur-Milchior/anki-clearer-empty-card"),
    Addon("Explain deletions", 12287769, 1556149013, "aa0d9485974fafd109ccd426f393a0d17aa94306", "https://github.com/Arthur-Milchior/anki-note-deletion"),
    Addon("Export cards selected in the Browser", 1983204951, 1560768960, "f8990da153af2745078e7b3c33854d01cb9fa304", "https://github.com/Arthur-Milchior/anki-export-from-browser"),
    Addon("Frozen Fields", 516643804, 1561600792, "191bbb759b3a9554e88fa36ba20b83fe68187f2d", "https://github.com/glutanimate/frozen-fields"),
    Addon("If a note has no more card warns instead of deleting it", 2018640062, 1560126140, "4a854242d06a05b2ca801a0afc29760682004782", "https://github.com/Arthur-Milchior/anki-keep-empty-note"),
    Addon("Improving change note type", 513858554, 1560753393, "4ece9f1da85358bce05a75d3bbeffa91d8c17ad4", "https://github.com/Arthur-Milchior/anki-change-note-type-clozes"),
    Addon("Improve speed of change of note type", 115825506, 1551823299, "8b125aa55a490b276019d1a2e7f6f8c0767d65b3", "https://github.com/Arthur-Milchior/anki-better-card-generation"),
    Addon("Keep model of add cards", 424778276, 1553438887, "64bdf3c7d8e252d6f69f0a423d2db3c23ce6bc04", "https://github.com/Arthur-Milchior/anki-keep-model-in-add-cards"),
    Addon("Limit number of cards by day both new and review", 602339056, 1562849512, "72f2ea268fa8b116f9aecde968dc0aa324b33636", "https://github.com/Arthur-Milchior/anki-limit-to-both-new-and-revs"),
    Addon("Long term backups", 529955533, gitHash="cf34f99c6c74e11b49928adb6876123fd1fa83dd", gitRepo = "https://github.com/Arthur-Milchior/anki-old-backup"),
    Addon("More consistent cards generation", 1713990897, 1562981270, "211e013581240d2f4a6b45e811d59adf17fc1862", "https://github.com/Arthur-Milchior/anki-correct-card-generation"),
    Addon("Multi-column note editor", 3491767031, 1560844854, "ad7a4014f184a1ec5d5d5c43a3fc4bab8bb8f6df", "https://github.com/hssm/anki-addons/tree/master/multi_column_editor"),
    Addon("Multi-column note editor debugged", 2064123047, 1550534156, "70f92cd5f62bd4feda5422701bd01acb41ed48ce", "https://github.com/Arthur-Milchior/anki-Multi-column-edit-window"),
    Addon("Newline in strings in add-ons configurations", 112201952, 1560116341, "c02ac9bbbc68212da3d2bccb65ad5599f9f5af97", "https://github.com/Arthur-Milchior/anki-json-new-line"),
    Addon("Open Added Today from Reviewer", 861864770, 1561610680, gitRepo = "https://github.com/glutanimate/anki-addons-misc"), #repo contains many add-ons. Thus hash seems useless. 47a218b21314f4ed7dd62397945c18fdfdfdff71
    Addon("Opening the same window multiple time", 354407385, 1545364194, "c832579f6ac7b327e16e6dfebcc513c1e89a693f", "https://github.com/Arthur-Milchior/anki-Multiple-Windows"),
    Addon("Postpone cards review", 1152543397, 1560126139, "27103fd69c19e0576c5df6e28b5687a8a3e3d905", "https://github.com/Arthur-Milchior/Anki-postpone-reviews"),
}

incorporatedAddonsDict = {**{addon.name: addon for addon in incorporatedAddonsSet if addon.name},
                          **{addon.id: addon for addon in incorporatedAddonsSet if addon.id}}
