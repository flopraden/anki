As (../../FILES.md), this document contains a succint description of
the content of each file from this folder.

# __init__.py

This only contains Importers, a list of pair, associating to each
description the importer object which allow to import this kind of
file.


# base
This file contains the definition of the class Importer. This class is
then inherited to allow the importation of any kind of files.

# Kind of files to import

## apkg
This is used to import a apkg files. Those are files created when
exporting a deck in anki. This is actually used for .apkg, .colpkg and
.zip files.

### anki2
If the package was generated with anki2 and not 2.1, the importer used
comes from this file.

## csvfile
This file contains code used to import a CSV file. Note that this does
not contains the code used to generate the window allowing to
configure the importation. The extension of a CSV file may be anything.

## pauker
This is used to import lessons from Pauker 1.8. Those are .pau.gz
files.

## Mnemo
This contains the code used to import Mnemosyne files. This file is
supposed to be a sqlite database with extension .db


## supermemo_xml
As indicated by the name, this file contains an importer for xml files
generated by supermemo, with extension .xml

## noteimp
This file allow to import a note. Those notes are supposed to have
been outputted using notes in plain text.