# (Be in -*- python -*- mode.)
#
# ====================================================================
# Copyright (c) 2000-2007 CollabNet.  All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution.  The terms
# are also available at http://subversion.tigris.org/license-1.html.
# If newer versions of this license are posted there, you may use a
# newer version instead, at your option.
#
# This software consists of voluntary contributions made by many
# individuals.  For exact contribution history, see the revision
# history and logs, available at http://cvs2svn.tigris.org/.
# ====================================================================

"""Data collection classes.

This module contains the code used to collect data from the CVS
repository.  It parses *,v files, recording all useful information
except for the actual file contents (though even the file contents
might be recorded by the RevisionRecorder if one is configured).

As a *,v file is parsed, the information pertaining to the file is
accumulated in memory, mostly in _RevisionData, _BranchData, and
_TagData objects.  When parsing is complete, a final pass is made over
the data to create some final dependency links, collect statistics,
etc., then the _*Data objects are converted into CVSItem objects
(CVSRevision, CVSBranch, and CVSTag respectively) and the CVSItems are
dumped into databases.

During the data collection, persistent unique ids are allocated to
many types of objects: CVSFile, Symbol, and CVSItems.  CVSItems are a
special case.  CVSItem ids are unique across all CVSItem types, and
the ids are carried over from the corresponding data collection
objects:

    _RevisionData -> CVSRevision

    _BranchData -> CVSBranch

    _TagData -> CVSTag

In a later pass it is possible to convert tags <-> branches.  But even
if this occurs, the new branch or tag uses the same id as the old tag
or branch.

"""


from __future__ import generators

import sys
import os
import stat
import re
import time

from cvs2svn_lib.boolean import *
from cvs2svn_lib.set_support import *
from cvs2svn_lib import config
from cvs2svn_lib.common import DB_OPEN_NEW
from cvs2svn_lib.common import FatalError
from cvs2svn_lib.common import warning_prefix
from cvs2svn_lib.common import error_prefix
from cvs2svn_lib.common import verify_svn_filename_legal
from cvs2svn_lib.common import path_split
from cvs2svn_lib.log import Log
from cvs2svn_lib.context import Ctx
from cvs2svn_lib.artifact_manager import artifact_manager
from cvs2svn_lib.project import FileInAndOutOfAtticException
from cvs2svn_lib.cvs_file import CVSPath
from cvs2svn_lib.cvs_file import CVSDirectory
from cvs2svn_lib.cvs_file import CVSFile
from cvs2svn_lib.symbol import Symbol
from cvs2svn_lib.symbol import Trunk
from cvs2svn_lib.cvs_item import CVSBranch
from cvs2svn_lib.cvs_item import CVSTag
from cvs2svn_lib.cvs_item import cvs_revision_type_map
from cvs2svn_lib.cvs_file_items import VendorBranchError
from cvs2svn_lib.cvs_file_items import CVSFileItems
from cvs2svn_lib.key_generator import KeyGenerator
from cvs2svn_lib.cvs_item_database import NewCVSItemStore
from cvs2svn_lib.symbol_statistics import SymbolStatisticsCollector
from cvs2svn_lib.metadata_database import MetadataDatabase

import cvs2svn_rcsparse


branch_tag_re = re.compile(r'''
    ^
    ((?:\d+\.\d+\.)+)   # A nonzero even number of digit groups w/trailing dot
    (?:0\.)?            # CVS sticks an extra 0 here; RCS does not
    (\d+)               # And the last digit group
    $
    ''', re.VERBOSE)


def rev_tuple(rev):
  """Return a tuple of integers corresponding to revision number REV.

  For example, if REV is '1.2.3.4', then return (1,2,3,4)."""

  return tuple([int(x) for x in rev.split('.')])


def is_trunk_revision(rev):
  """Return True iff REV is a trunk revision.

  REV is a revision number corresponding to a specific revision (i.e.,
  not a whole branch)."""

  return rev.count('.') == 1


def is_same_line_of_development(rev1, rev2):
  """Return True if rev1 and rev2 are on the same line of
  development (i.e., both on trunk, or both on the same branch);
  return False otherwise.  Either rev1 or rev2 can be None, in
  which case automatically return False."""

  if rev1 is None or rev2 is None:
    return False
  if rev1.count('.') == 1 and rev2.count('.') == 1:
    return True
  if rev1[0:rev1.rfind('.')] == rev2[0:rev2.rfind('.')]:
    return True
  return False


class _RevisionData:
  """We track the state of each revision so that in set_revision_info,
  we can determine if our op is an add/change/delete.  We can do this
  because in set_revision_info, we'll have all of the _RevisionData
  for a file at our fingertips, and we need to examine the state of
  our prev_rev to determine if we're an add or a change.  Without the
  state of the prev_rev, we are unable to distinguish between an add
  and a change."""

  def __init__(self, cvs_rev_id, rev, timestamp, author, state):
    # The id of this revision:
    self.cvs_rev_id = cvs_rev_id
    self.rev = rev
    self.timestamp = timestamp
    self.author = author
    self.original_timestamp = timestamp
    self.state = state

    # If this is the first revision on a branch, then this is the
    # branch_data of that branch; otherwise it is None.
    self.parent_branch_data = None

    # The revision number of the parent of this revision along the
    # same line of development, if any.  For the first revision R on a
    # branch, we consider the revision from which R sprouted to be the
    # 'parent'.  If this is the root revision in the file's revision
    # tree, then this field is None.
    #
    # Note that this revision can't be determined arithmetically (due
    # to cvsadmin -o), which is why this field is necessary.
    self.parent = None

    # The revision number of the primary child of this revision (the
    # child along the same line of development), if any; otherwise,
    # None.
    self.child = None

    # The _BranchData instances of branches that sprout from this
    # revision, sorted in ascending order by branch number.  It would
    # be inconvenient to initialize it here because we would have to
    # scan through all branches known by the _SymbolDataCollector to
    # find the ones having us as the parent.  Instead, this
    # information is filled in by
    # _FileDataCollector._resolve_dependencies() and sorted by
    # _FileDataCollector._sort_branches().
    self.branches_data = []

    # The revision numbers of the first commits on any branches on
    # which commits occurred.  This dependency is kept explicitly
    # because otherwise a revision-only topological sort would miss
    # the dependency that exists via branches_data.
    self.branches_revs_data = []

    # The _TagData instances of tags that are connected to this
    # revision.
    self.tags_data = []

    # A token that may be returned from
    # RevisionRecorder.record_text().  It can be used by
    # RevisionReader to obtain the text again.
    self.revision_recorder_token = None

  def get_first_on_branch_id(self):
    return self.parent_branch_data and self.parent_branch_data.id


class _SymbolData:
  """Collection area for information about a symbol in a single CVSFile.

  SYMBOL is an instance of Symbol, undifferentiated as a Branch or a
  Tag regardless of whether self is a _BranchData or a _TagData."""

  def __init__(self, id, symbol):
    """Initialize an object for SYMBOL."""

    # The unique id that will be used for this particular symbol in
    # this particular file.  This same id will be used for the CVSItem
    # that is derived from this instance.
    self.id = id

    # An instance of Symbol.
    self.symbol = symbol


class _BranchData(_SymbolData):
  """Collection area for information about a Branch in a single CVSFile."""

  def __init__(self, id, symbol, branch_number):
    _SymbolData.__init__(self, id, symbol)

    # The branch number (e.g., '1.5.2') of this branch.
    self.branch_number = branch_number

    # The revision number of the revision from which this branch
    # sprouts (e.g., '1.5').
    self.parent = self.branch_number[:self.branch_number.rindex(".")]

    # The revision number of the first commit on this branch, if any
    # (e.g., '1.5.2.1'); otherwise, None.
    self.child = None


class _TagData(_SymbolData):
  """Collection area for information about a Tag in a single CVSFile."""

  def __init__(self, id, symbol, rev):
    _SymbolData.__init__(self, id, symbol)

    # The revision number being tagged (e.g., '1.5.2.3').
    self.rev = rev


class _SymbolDataCollector(object):
  """Collect information about symbols in a single CVSFile."""

  def __init__(self, fdc, cvs_file):
    self.fdc = fdc
    self.cvs_file = cvs_file

    self.pdc = self.fdc.pdc
    self.collect_data = self.fdc.collect_data

    # A set containing the names of each known symbol in this file,
    # used to check for duplicates.
    self._known_symbols = set()

    # Map { branch_number : _BranchData }, where branch_number has an
    # odd number of digits.
    self.branches_data = { }

    # Map { revision : [ tag_data ] }, where revision has an even
    # number of digits, and the value is a list of _TagData objects
    # for tags that apply to that revision.
    self.tags_data = { }

  def _add_branch(self, name, branch_number):
    """Record that BRANCH_NUMBER is the branch number for branch NAME,
    and derive and record the revision from which NAME sprouts.
    BRANCH_NUMBER is an RCS branch number with an odd number of
    components, for example '1.7.2' (never '1.7.0.2').  Return the
    _BranchData instance (which is usually newly-created)."""

    branch_data = self.branches_data.get(branch_number)

    if branch_data is not None:
      Log().warn(
          "%s: in '%s':\n"
          "   branch '%s' already has name '%s',\n"
          "   cannot also have name '%s', ignoring the latter\n"
          % (warning_prefix,
             self.cvs_file.filename, branch_number,
             branch_data.symbol.name, name)
          )
      return branch_data

    symbol = self.pdc.get_symbol(name)
    branch_data = _BranchData(
        self.collect_data.item_key_generator.gen_id(), symbol, branch_number
        )
    self.branches_data[branch_number] = branch_data
    return branch_data

  def _add_unlabeled_branch(self, branch_number):
    name = "unlabeled-" + branch_number
    return self._add_branch(name, branch_number)

  def _add_tag(self, name, revision):
    """Record that tag NAME refers to the specified REVISION."""

    symbol = self.pdc.get_symbol(name)
    tag_data = _TagData(
        self.collect_data.item_key_generator.gen_id(), symbol, revision
        )
    self.tags_data.setdefault(revision, []).append(tag_data)
    return tag_data

  def define_symbol(self, name, revision):
    """Record a symbol called NAME, which is associated with REVISON.

    REVISION is an unprocessed revision number from the RCS file's
    header, for example: '1.7', '1.7.0.2', or '1.1.1' or '1.1.1.1'.
    NAME is an untransformed branch or tag name.  This function will
    determine by inspection whether it is a branch or a tag, and
    record it in the right places."""

    # Determine whether it is a branch or tag, and canonicalize the
    # revision number:
    m = branch_tag_re.match(revision)
    if m:
      is_branch = True
      revision = m.group(1) + m.group(2)
    else:
      is_branch = False

    name = self.cvs_file.project.transform_symbol(
        self.cvs_file, name, revision, is_branch
        )

    if name is None:
      # Ignore this symbol
      pass
    elif name in self._known_symbols:
      # The symbol is already defined.  This can easily happen when
      # --symbol-transform is used:
      self.collect_data.record_fatal_error(
          "Multiple definitions of the symbol '%s' in '%s'"
          % (name, self.cvs_file.filename)
          )
    else:
      # Add symbol to our records:
      self._known_symbols.add(name)
      if is_branch:
        self._add_branch(name, revision)
      else:
        self._add_tag(name, revision)

  def rev_to_branch_number(revision):
    """Return the branch_number of the branch on which REVISION lies.

    REVISION is a branch revision number with an even number of
    components; for example '1.7.2.1' (never '1.7.2' nor '1.7.0.2').
    The return value is the branch number (for example, '1.7.2').
    Return none iff REVISION is a trunk revision such as '1.2'."""

    if is_trunk_revision(revision):
      return None
    return revision[:revision.rindex(".")]

  rev_to_branch_number = staticmethod(rev_to_branch_number)

  def rev_to_branch_data(self, revision):
    """Return the branch_data of the branch on which REVISION lies.

    REVISION must be a branch revision number with an even number of
    components; for example '1.7.2.1' (never '1.7.2' nor '1.7.0.2').
    Raise KeyError iff REVISION is unknown."""

    assert not is_trunk_revision(revision)

    return self.branches_data[self.rev_to_branch_number(revision)]

  def rev_to_lod(self, revision):
    """Return the line of development on which REVISION lies.

    REVISION must be a revision number with an even number of
    components.  Raise KeyError iff REVISION is unknown."""

    if is_trunk_revision(revision):
      return self.pdc.trunk
    else:
      return self.rev_to_branch_data(revision).symbol


class _FileDataCollector(cvs2svn_rcsparse.Sink):
  """Class responsible for collecting RCS data for a particular file.

  Any collected data that need to be remembered are stored into the
  referenced CollectData instance."""

  def __init__(self, pdc, cvs_file):
    """Create an object that is prepared to receive data for CVS_FILE.
    CVS_FILE is a CVSFile instance.  COLLECT_DATA is used to store the
    information collected about the file."""

    self.pdc = pdc
    self.cvs_file = cvs_file

    self.collect_data = self.pdc.collect_data
    self.project = self.cvs_file.project

    # A list [(name, revision), ...] of symbols defined in the header
    # of the file.  This list is processed then deleted in
    # admin_completed().
    self._symbol_defs = []

    # A place to store information about the symbols in this file:
    self.sdc = _SymbolDataCollector(self, self.cvs_file)

    # { revision : _RevisionData instance }
    self._rev_data = { }

    # Lists [ (parent, child) ] of revision number pairs indicating
    # that revision child depends on revision parent along the main
    # line of development.
    self._primary_dependencies = []

    # If set, this is an RCS branch number -- rcsparse calls this the
    # "principal branch", but CVS and RCS refer to it as the "default
    # branch", so that's what we call it, even though the rcsparse API
    # setter method is still 'set_principal_branch'.
    self.default_branch = None

    # True iff revision 1.1 of the file appears to have been imported
    # (as opposed to added normally).
    self._file_imported = False

  def _get_rev_id(self, revision):
    if revision is None:
      return None
    return self._rev_data[revision].cvs_rev_id

  def set_principal_branch(self, branch):
    """This is a callback method declared in Sink."""

    self.default_branch = branch

  def set_expansion(self, mode):
    """This is a callback method declared in Sink."""

    self.cvs_file.mode = mode

  def define_tag(self, name, revision):
    """Remember the symbol name and revision, but don't process them yet.

    This is a callback method declared in Sink."""

    self._symbol_defs.append((name, revision))

  def admin_completed(self):
    """This is a callback method declared in Sink."""

    for (name, revision) in self._symbol_defs:
      self.sdc.define_symbol(name, revision)

    del self._symbol_defs

  def define_revision(self, revision, timestamp, author, state,
                      branches, next):
    """This is a callback method declared in Sink."""

    for branch in branches:
      try:
        branch_data = self.sdc.rev_to_branch_data(branch)
      except KeyError:
        # Normally we learn about the branches from the branch names
        # and numbers parsed from the symbolic name header.  But this
        # must have been an unlabeled branch that slipped through the
        # net.  Generate a name for it and create a _BranchData record
        # for it now.
        branch_data = self.sdc._add_unlabeled_branch(
            self.sdc.rev_to_branch_number(branch))

      assert branch_data.child is None
      branch_data.child = branch

    if revision in self._rev_data:
      # This revision has already been seen.
      raise FatalError(
          'File %r contains duplicate definitions of revision %s.'
          % (self.cvs_file.filename, revision,))

    # Record basic information about the revision:
    rev_data = _RevisionData(
        self.collect_data.item_key_generator.gen_id(),
        revision, int(timestamp), author, state)
    self._rev_data[revision] = rev_data

    # When on trunk, the RCS 'next' revision number points to what
    # humans might consider to be the 'previous' revision number.  For
    # example, 1.3's RCS 'next' is 1.2.
    #
    # However, on a branch, the RCS 'next' revision number really does
    # point to what humans would consider to be the 'next' revision
    # number.  For example, 1.1.2.1's RCS 'next' would be 1.1.2.2.
    #
    # In other words, in RCS, 'next' always means "where to find the next
    # deltatext that you need this revision to retrieve.
    #
    # That said, we don't *want* RCS's behavior here, so we determine
    # whether we're on trunk or a branch and set the dependencies
    # accordingly.
    if next:
      if is_trunk_revision(revision):
        self._primary_dependencies.append( (next, revision,) )
      else:
        self._primary_dependencies.append( (revision, next,) )

  def _resolve_primary_dependencies(self):
    """Resolve the dependencies listed in self._primary_dependencies."""

    for (parent, child,) in self._primary_dependencies:
      parent_data = self._rev_data[parent]
      assert parent_data.child is None
      parent_data.child = child

      child_data = self._rev_data[child]
      assert child_data.parent is None
      child_data.parent = parent

  def _resolve_branch_dependencies(self):
    """Resolve dependencies involving branches."""

    for branch_data in self.sdc.branches_data.values():
      # The branch_data's parent has the branch as a child regardless
      # of whether the branch had any subsequent commits:
      try:
        parent_data = self._rev_data[branch_data.parent]
      except KeyError:
        Log().warn(
            'In %r:\n'
            '    branch %r references non-existing revision %s\n'
            '    and will be ignored.'
            % (self.cvs_file.filename, branch_data.symbol.name,
               branch_data.parent,))
        del self.sdc.branches_data[branch_data.branch_number]
      else:
        parent_data.branches_data.append(branch_data)

        # If the branch has a child (i.e., something was committed on
        # the branch), then we store a reference to the branch_data
        # there, define the child's parent to be the branch's parent,
        # and list the child in the branch parent's branches_revs_data:
        if branch_data.child is not None:
          child_data = self._rev_data[branch_data.child]
          assert child_data.parent_branch_data is None
          child_data.parent_branch_data = branch_data
          assert child_data.parent is None
          child_data.parent = branch_data.parent
          parent_data.branches_revs_data.append(branch_data.child)

  def _sort_branches(self):
    """Sort the branches sprouting from each revision in creation order.

    Creation order is taken to be the reverse of the order that they
    are listed in the symbols part of the RCS file.  (If a branch is
    created then deleted, a later branch can be assigned the recycled
    branch number; therefore branch numbers are not an indication of
    creation order.)"""

    for rev_data in self._rev_data.values():
      rev_data.branches_data.sort(lambda a, b: - cmp(a.id, b.id))

  def _resolve_tag_dependencies(self):
    """Resolve dependencies involving tags."""

    for (rev, tag_data_list) in self.sdc.tags_data.items():
      try:
        parent_data = self._rev_data[rev]
      except KeyError:
        Log().warn(
            'In %r:\n'
            '    the following tag(s) reference non-existing revision %s\n'
            '    and will be ignored:\n'
            '    %s' % (
                self.cvs_file.filename, rev,
                ', '.join([repr(tag_data.symbol.name)
                           for tag_data in tag_data_list]),))
        del self.sdc.tags_data[rev]
      else:
        for tag_data in tag_data_list:
          assert tag_data.rev == rev
          # The tag_data's rev has the tag as a child:
          parent_data.tags_data.append(tag_data)

  def _determine_operation(self, rev_data):
    prev_rev_data = self._rev_data.get(rev_data.parent)
    return cvs_revision_type_map[(
        rev_data.state != 'dead',
        prev_rev_data is not None and prev_rev_data.state != 'dead',
        )]

  def _get_cvs_revision(self, rev_data):
    """Create and return a CVSRevision for REV_DATA."""

    branch_ids = [
        branch_data.id
        for branch_data in rev_data.branches_data
        ]

    branch_commit_ids = [
        self._get_rev_id(rev)
        for rev in rev_data.branches_revs_data
        ]

    tag_ids = [
        tag_data.id
        for tag_data in rev_data.tags_data
        ]

    revision_type = self._determine_operation(rev_data)

    return revision_type(
        self._get_rev_id(rev_data.rev), self.cvs_file,
        rev_data.timestamp, None,
        self._get_rev_id(rev_data.parent),
        self._get_rev_id(rev_data.child),
        rev_data.rev,
        True,
        self.sdc.rev_to_lod(rev_data.rev),
        rev_data.get_first_on_branch_id(),
        False, None, None,
        tag_ids, branch_ids, branch_commit_ids,
        rev_data.revision_recorder_token)

  def _get_cvs_revisions(self):
    """Generate the CVSRevisions present in this file."""

    for rev_data in self._rev_data.itervalues():
      yield self._get_cvs_revision(rev_data)

  def _get_cvs_branches(self):
    """Generate the CVSBranches present in this file."""

    for branch_data in self.sdc.branches_data.values():
      yield CVSBranch(
          branch_data.id, self.cvs_file, branch_data.symbol,
          branch_data.branch_number,
          self.sdc.rev_to_lod(branch_data.parent),
          self._get_rev_id(branch_data.parent),
          self._get_rev_id(branch_data.child),
          None,
          )

  def _get_cvs_tags(self):
    """Generate the CVSTags present in this file."""

    for tags_data in self.sdc.tags_data.values():
      for tag_data in tags_data:
        yield CVSTag(
            tag_data.id, self.cvs_file, tag_data.symbol,
            self.sdc.rev_to_lod(tag_data.rev),
            self._get_rev_id(tag_data.rev),
            None,
            )

  def tree_completed(self):
    """The revision tree has been parsed.

    Analyze it for consistency and connect some loose ends.

    This is a callback method declared in Sink."""

    self._resolve_primary_dependencies()
    self._resolve_branch_dependencies()
    self._sort_branches()
    self._resolve_tag_dependencies()

    # Compute the preliminary CVSFileItems for this file:
    cvs_items = []
    cvs_items.extend(self._get_cvs_revisions())
    cvs_items.extend(self._get_cvs_branches())
    cvs_items.extend(self._get_cvs_tags())
    self._cvs_file_items = CVSFileItems(
        self.cvs_file, self.pdc.trunk, cvs_items
        )

    if Log().is_on(Log.DEBUG):
      self._cvs_file_items.check_link_consistency()

    # Tell the revision recorder about the file dependency tree.
    self.collect_data.revision_recorder.start_file(self._cvs_file_items)

  def set_revision_info(self, revision, log, text):
    """This is a callback method declared in Sink."""

    rev_data = self._rev_data[revision]
    cvs_rev = self._cvs_file_items[rev_data.cvs_rev_id]

    if cvs_rev.metadata_id is not None:
      # Users have reported problems with repositories in which the
      # deltatext block for revision 1.1 appears twice.  It is not
      # known whether this results from a CVS/RCS bug, or from botched
      # hand-editing of the repository.  In any case, empirically, cvs
      # and rcs both use the first version when checking out data, so
      # that's what we will do.  (For the record: "cvs log" fails on
      # such a file; "rlog" prints the log message from the first
      # block and ignores the second one.)
      Log().warn(
          "%s: in '%s':\n"
          "   Deltatext block for revision %s appeared twice;\n"
          "   ignoring the second occurrence.\n"
          % (warning_prefix, self.cvs_file.filename, revision,)
          )
      return

    if is_trunk_revision(revision):
      branch_name = None
    else:
      branch_name = self.sdc.rev_to_branch_data(revision).symbol.name

    cvs_rev.metadata_id = self.collect_data.metadata_db.get_key(
        self.project, branch_name, rev_data.author, log)
    cvs_rev.deltatext_exists = bool(text)

    # If this is revision 1.1, determine whether the file appears to
    # have been created via 'cvs add' instead of 'cvs import'.  The
    # test is that the log message CVS uses for 1.1 in imports is
    # "Initial revision\n" with no period.  (This fact helps determine
    # whether this file might have had a default branch in the past.)
    if revision == '1.1':
      self._file_imported = (log == 'Initial revision\n')

    cvs_rev.revision_recorder_token = \
        self.collect_data.revision_recorder.record_text(cvs_rev, log, text)

  def parse_completed(self):
    """Finish the processing of this file.

    This is a callback method declared in Sink."""

    pass

  def _process_ntdbrs(self):
    """Fix up any non-trunk default branch revisions (if present).

    If a non-trunk default branch is determined to have existed, yield
    the _RevisionData.ids for all revisions that were once non-trunk
    default revisions, in dependency order.

    There are two cases to handle:

    One case is simple.  The RCS file lists a default branch
    explicitly in its header, such as '1.1.1'.  In this case, we know
    that every revision on the vendor branch is to be treated as head
    of trunk at that point in time.

    But there's also a degenerate case.  The RCS file does not
    currently have a default branch, yet we can deduce that for some
    period in the past it probably *did* have one.  For example, the
    file has vendor revisions 1.1.1.1 -> 1.1.1.96, all of which are
    dated before 1.2, and then it has 1.1.1.97 -> 1.1.1.100 dated
    after 1.2.  In this case, we should record 1.1.1.96 as the last
    vendor revision to have been the head of the default branch.

    If any non-trunk default branch revisions are found:

    - Set their ntdbr members to True.

    - Connect the last one with revision 1.2.

    - Remove revision 1.1 if it is not needed.

    """

    try:
      if self.default_branch:
        vendor_cvs_branch_id = self.sdc.branches_data[self.default_branch].id
        vendor_lod_items = self._cvs_file_items.get_lod_items(
            self._cvs_file_items[vendor_cvs_branch_id]
            )
        if not self._cvs_file_items.process_live_ntdb(vendor_lod_items):
          return
      elif self._file_imported:
        vendor_branch_data = self.sdc.branches_data.get('1.1.1')
        if vendor_branch_data is None:
          return
        else:
          vendor_lod_items = self._cvs_file_items.get_lod_items(
              self._cvs_file_items[vendor_branch_data.id]
              )
          if not self._cvs_file_items.process_historical_ntdb(
                vendor_lod_items
                ):
            return
      else:
        return
    except VendorBranchError, e:
      self.collect_data.record_fatal_error(str(e))
      return

    if self._file_imported:
      self._cvs_file_items.imported_remove_1_1(vendor_lod_items)

    if Log().is_on(Log.DEBUG):
      self._cvs_file_items.check_link_consistency()

  def get_cvs_file_items(self):
    """Finish up and return a CVSFileItems instance for this file.

    This method must only be called once."""

    self._process_ntdbrs()

    # Break a circular reference loop, allowing the memory for self
    # and sdc to be freed.
    del self.sdc

    return self._cvs_file_items


class _ProjectDataCollector:
  def __init__(self, collect_data, project):
    self.collect_data = collect_data
    self.project = project
    self.found_rcs_file = False
    self.num_files = 0

    Ctx()._projects[project.id] = project

    # The Trunk LineOfDevelopment object for this project.
    self.trunk = Trunk(
        self.collect_data.symbol_key_generator.gen_id(), self.project
        )
    self.project.trunk_id = self.trunk.id

    # This causes a record for self.trunk to spring into existence:
    self.collect_data.symbol_stats[self.trunk]

    # A map { name -> Symbol } for all known symbols in this project.
    # The symbols listed here are undifferentiated into Branches and
    # Tags because the same name might appear as a branch in one file
    # and a tag in another.
    self.symbols = {}

    root_cvs_directory = CVSDirectory(
        self.collect_data.file_key_generator.gen_id(), self.project, None, ''
        )

    self.project.root_cvs_directory_id = root_cvs_directory.id

    self._visit_non_attic_directory(root_cvs_directory)

    if not self.found_rcs_file:
      self.collect_data.record_fatal_error(
          'No RCS files found under %r!\n'
          'Are you absolutely certain you are pointing cvs2svn\n'
          'at a CVS repository?\n'
          % (self.project.project_cvs_repos_path,)
          )

  def get_symbol(self, name):
    """Return the Symbol object for the symbol named NAME in this project.

    If such a symbol does not yet exist, allocate a new symbol_id,
    create a Symbol instance, store it in self.symbols, and return it."""

    symbol = self.symbols.get(name)
    if symbol is None:
      symbol = Symbol(
          self.collect_data.symbol_key_generator.gen_id(),
          self.project, name)
      self.symbols[name] = symbol
    return symbol

  def _process_file(self, cvs_file):
    Log().normal(cvs_file.filename)
    fdc = _FileDataCollector(self, cvs_file)
    try:
      cvs2svn_rcsparse.parse(open(cvs_file.filename, 'rb'), fdc)
    except (cvs2svn_rcsparse.common.RCSParseError, ValueError, RuntimeError):
      self.collect_data.record_fatal_error(
          "%r is not a valid ,v file" % (cvs_file.filename,)
          )
    except:
      Log().warn("Exception occurred while parsing %s" % cvs_file.filename)
      raise
    else:
      self.num_files += 1

    cvs_file_items = fdc.get_cvs_file_items()

    del fdc

    # Remove CVSRevisionDeletes that are not needed:
    cvs_file_items.remove_unneeded_deletes(self.collect_data.metadata_db)

    # Remove initial branch deletes that are not needed:
    cvs_file_items.remove_initial_branch_deletes(
        self.collect_data.metadata_db
        )

    # If this is a --trunk-only conversion, discard all branches and
    # tags, then draft any non-trunk default branch revisions to
    # trunk:
    if Ctx().trunk_only:
      cvs_file_items.exclude_non_trunk()

    self.collect_data.revision_recorder.finish_file(cvs_file_items)
    self.collect_data.add_cvs_file_items(cvs_file_items)
    self.collect_data.symbol_stats.register(cvs_file_items)

  def _get_cvs_file(
        self, parent_directory, basename, file_in_attic, leave_in_attic=False
        ):
    """Return a CVSFile describing the file with name BASENAME.

    PARENT_DIRECTORY is the CVSDirectory instance describing the
    directory that physically holds this file in the filesystem.
    BASENAME must be the base name of a *,v file within
    PARENT_DIRECTORY.

    FILE_IN_ATTIC is a boolean telling whether the specified file is
    in an Attic subdirectory.  If FILE_IN_ATTIC is True, then:

    - If LEAVE_IN_ATTIC is True, then leave the 'Attic' component in
      the filename.

    - Otherwise, raise FileInAndOutOfAtticException if a file with the
      same filename appears outside of Attic.

    The CVSFile is assigned a new unique id.  All of the CVSFile
    information is filled in except mode (which can only be determined
    by parsing the file).

    Raise FatalError if the resulting filename would not be legal in
    SVN."""

    filename = os.path.join(parent_directory.filename, basename)
    verify_svn_filename_legal(filename, basename[:-2])

    if file_in_attic and not leave_in_attic:
      in_attic = True
      logical_parent_directory = parent_directory.parent_directory

      # If this file also exists outside of the attic, it's a fatal
      # error:
      non_attic_filename = os.path.join(
          logical_parent_directory.filename, basename,
          )
      if os.path.exists(non_attic_filename):
        raise FileInAndOutOfAtticException(non_attic_filename, filename)
    else:
      in_attic = False
      logical_parent_directory = parent_directory

    file_stat = os.stat(filename)

    # The size of the file in bytes:
    file_size = file_stat[stat.ST_SIZE]

    # Whether or not the executable bit is set:
    file_executable = bool(file_stat[0] & stat.S_IXUSR)

    # mode is not known, so we temporarily set it to None.
    return CVSFile(
        self.collect_data.file_key_generator.gen_id(),
        self.project, logical_parent_directory, basename[:-2], in_attic,
        file_executable, file_size, None
        )

  def _get_attic_file(self, parent_directory, basename):
    """Return a CVSFile object for the Attic file at BASENAME.

    PARENT_DIRECTORY is the CVSDirectory that physically contains the
    file on the filesystem (i.e., the Attic directory).  It is not
    necessarily the parent_directory of the CVSFile that will be
    returned.

    Return (CVSFile, retained_in_attic), where RETAINED_IN_ATTIC is a
    boolean that is True iff CVSFile will remain in the Attic
    directory."""

    try:
      return (self._get_cvs_file(parent_directory, basename, True), False)
    except FileInAndOutOfAtticException, e:
      if Ctx().retain_conflicting_attic_files:
        Log().warn(
            "%s: %s;\n"
            "   storing the latter into 'Attic' subdirectory.\n"
            % (warning_prefix, e)
            )
      else:
        self.collect_data.record_fatal_error(str(e))

      # Either way, return a CVSFile object so that the rest of the
      # file processing can proceed:
      return (
          self._get_cvs_file(
              parent_directory, basename, True, leave_in_attic=True
              ),
          True,
          )

  def _visit_attic_directory(self, cvs_directory):
    """Visit the Attic directory CVS_DIRECTORY."""

    # Maps { fname[:-2] : pathname }:
    rcsfiles = {}

    retained_attic_file = False

    for fname in os.listdir(cvs_directory.filename):
      pathname = os.path.join(cvs_directory.filename, fname)
      if os.path.isdir(pathname):
        Log().warn("Directory %s found within Attic; ignoring" % (pathname,))
      elif fname.endswith(',v'):
        self.found_rcs_file = True
        rcsfiles[fname[:-2]] = pathname
        (cvs_file, retained_in_attic) = self._get_attic_file(
            cvs_directory, fname
            )
        retained_attic_file |= retained_in_attic
        self._process_file(cvs_file)

    if retained_attic_file:
      # If any files were retained in the Attic directory, then write
      # the Attic directory to CVSFileDatabase:
      self.collect_data.add_cvs_directory(cvs_directory)

    return rcsfiles

  def _get_non_attic_file(self, parent_directory, basename):
    """Return a CVSFile object for the non-Attic file at BASENAME."""

    return self._get_cvs_file(parent_directory, basename, False)

  def _visit_non_attic_directory(self, cvs_directory):
    """Visit the non-Attic directory CVS_DIRECTORY."""

    self.collect_data.add_cvs_directory(cvs_directory)

    files = os.listdir(cvs_directory.filename)

    # Map { fname[:-2] : pathname }:
    rcsfiles = {}

    attic_dir = None

    dirs = []

    for fname in files[:]:
      pathname = os.path.join(cvs_directory.filename, fname)
      if os.path.isdir(pathname):
        if fname == 'Attic':
          attic_dir = fname
        else:
          dirs.append(fname)
      elif fname.endswith(',v'):
        self.found_rcs_file = True
        rcsfiles[fname[:-2]] = pathname
        self._process_file(self._get_non_attic_file(cvs_directory, fname))
      else:
        # Silently ignore other files:
        pass

    if attic_dir is not None:
      attic_directory = CVSDirectory(
          self.collect_data.file_key_generator.gen_id(),
          self.project, cvs_directory, 'Attic',
          )

      attic_rcsfiles = self._visit_attic_directory(attic_directory)
      alldirs = dirs + [attic_dir]
    else:
      alldirs = dirs
      attic_rcsfiles = {}

    # Check for conflicts between directory names and the filenames
    # that will result from the rcs files (both in this directory and
    # in attic).  (We recurse into the subdirectories nevertheless, to
    # try to detect more problems.)
    for fname in alldirs:
      pathname = os.path.join(cvs_directory.filename, fname)
      for rcsfile_list in [rcsfiles, attic_rcsfiles]:
        if fname in rcsfile_list:
          self.collect_data.record_fatal_error(
              'Directory name conflicts with filename.  Please remove or '
              'rename one\n'
              'of the following:\n'
              '    "%s"\n'
              '    "%s"'
              % (pathname, rcsfile_list[fname],)
              )

    # Now recurse into the other subdirectories:
    for fname in dirs:
      dirname = os.path.join(cvs_directory.filename, fname)

      # Verify that the directory name does not contain any illegal
      # characters:
      verify_svn_filename_legal(dirname, fname)

      sub_directory = CVSDirectory(
          self.collect_data.file_key_generator.gen_id(),
          self.project, cvs_directory, fname,
          )

      self._visit_non_attic_directory(sub_directory)


class CollectData:
  """Repository for data collected by parsing the CVS repository files.

  This class manages the databases into which information collected
  from the CVS repository is stored.  The data are stored into this
  class by _FileDataCollector instances, one of which is created for
  each file to be parsed."""

  def __init__(self, revision_recorder, stats_keeper):
    self.revision_recorder = revision_recorder
    self._cvs_item_store = NewCVSItemStore(
        artifact_manager.get_temp_file(config.CVS_ITEMS_STORE))
    self.metadata_db = MetadataDatabase(DB_OPEN_NEW)
    self.fatal_errors = []
    self.num_files = 0
    self.symbol_stats = SymbolStatisticsCollector()
    self.stats_keeper = stats_keeper

    # Key generator for CVSFiles:
    self.file_key_generator = KeyGenerator()

    # Key generator for CVSItems:
    self.item_key_generator = KeyGenerator()

    # Key generator for Symbols:
    self.symbol_key_generator = KeyGenerator()

    self.revision_recorder.start()

  def record_fatal_error(self, err):
    """Record that fatal error ERR was found.

    ERR is a string (without trailing newline) describing the error.
    Output the error to stderr immediately, and record a copy to be
    output again in a summary at the end of CollectRevsPass."""

    err = '%s: %s' % (error_prefix, err,)
    sys.stderr.write(err + '\n')
    self.fatal_errors.append(err)

  def process_project(self, project):
    pdc = _ProjectDataCollector(self, project)
    self.num_files += pdc.num_files
    Log().verbose('Processed', self.num_files, 'files')

  def add_cvs_directory(self, cvs_directory):
    """Record CVS_DIRECTORY."""

    Ctx()._cvs_file_db.log_file(cvs_directory)

  def add_cvs_file_items(self, cvs_file_items):
    """Record the information from CVS_FILE_ITEMS.

    Store the CVSFile to _cvs_file_db under its persistent id, store
    the CVSItems, and record the CVSItems to self.stats_keeper."""

    Ctx()._cvs_file_db.log_file(cvs_file_items.cvs_file)
    self._cvs_item_store.add(cvs_file_items)

    self.stats_keeper.record_cvs_file(cvs_file_items.cvs_file)
    for cvs_item in cvs_file_items.values():
      self.stats_keeper.record_cvs_item(cvs_item)

  def _set_cvs_path_ordinals(self):
    cvs_files = list(Ctx()._cvs_file_db.itervalues())
    cvs_files.sort(CVSPath.slow_compare)
    for i in range(len(cvs_files)):
      cvs_files[i].ordinal = i

  def close(self):
    """Close the data structures associated with this instance.

    Return a list of fatal errors encountered while processing input.
    Each list entry is a string describing one fatal error."""

    self.revision_recorder.finish()
    self.symbol_stats.purge_ghost_symbols()
    self.symbol_stats.close()
    self.symbol_stats = None
    self.metadata_db.close()
    self.metadata_db = None
    self._cvs_item_store.close()
    self._cvs_item_store = None
    self._set_cvs_path_ordinals()
    self.revision_recorder = None
    retval = self.fatal_errors
    self.fatal_errors = None
    return retval


