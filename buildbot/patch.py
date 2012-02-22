# Copyright (c) 2011 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Module that handles the processing of patches to the source tree."""

import constants
import glob
import logging
import os
import re
import shutil
import tempfile

from chromite.lib import cros_build_lib as cros_lib

# The prefix of the temporary directory created to store local patches.
_TRYBOT_TEMP_PREFIX = 'trybot_patch-'


class PatchException(Exception):
  """Exception thrown by GetGerritPatchInfo."""

class ApplyPatchException(Exception):
  """Exception thrown if we fail to apply a patch."""
  # Types used to denote what we failed to apply against.
  TYPE_REBASE_TO_TOT = 1
  TYPE_REBASE_TO_PATCH_INFLIGHT = 2

  def __init__(self, patch, patch_type=TYPE_REBASE_TO_TOT):
    super(ApplyPatchException, self).__init__()
    self.patch = patch
    self.type = patch_type

  def __str__(self):
    return 'Failed to apply patch ' + str(self.patch)


class MissingChangeIDException(Exception):
  """Raised if a patch is missing a Change-ID."""
  pass


class Patch(object):
  """Abstract class representing a Git Patch."""

  def __init__(self, project, tracking_branch):
    """Initialization of abstract Patch class.

    Args:
      project: The name of the project that the patch applies to.
      tracking_branch:  The remote branch of the project the patch applies to.
    """
    self.project = project
    self.tracking_branch = tracking_branch

  def ProjectDir(self, buildroot):
    """Returns the local directory where this patch will be applied."""
    return cros_lib.GetProjectDir(buildroot, self.project)

  def Apply(self, buildroot, trivial):
    """Applies the patch to specified buildroot. Implement in subclasses.

    Args:
      buildroot:  The buildroot.
      trivial:  Only allow trivial merges when applying change.

    Raises:
      PatchException
    """
    raise NotImplementedError('Applies the patch to specified buildroot.')


class GerritPatch(Patch):
  """Object that represents a Gerrit CL."""
  _PUBLIC_URL = os.path.join(constants.GERRIT_HTTP_URL, 'gerrit/p')
  _GIT_CHANGE_ID_RE = re.compile(r'^\s*Change-Id:\s*(\w+)\s*$', re.MULTILINE)
  _PALADIN_DEPENDENCY_RE = re.compile(r'^\s*CQ-DEPEND=(.*)$', re.MULTILINE)
  _PALADIN_BUG_RE = re.compile('(\w+)')

  def __init__(self, patch_dict, internal):
    """Construct a GerritPatch object from Gerrit query results.

    Gerrit query JSON fields are documented at:
    http://gerrit-documentation.googlecode.com/svn/Documentation/2.2.1/json.html

    Args:
      patch_dict: A dictionary containing the parsed JSON gerrit query results.
      internal: Whether the CL is an internal CL.
    """
    super(GerritPatch, self).__init__(patch_dict['project'],
                                      patch_dict['branch'])
    self.patch_dict = patch_dict
    self.internal = internal
    # id - The CL's ChangeId
    self.id = patch_dict['id']
    # ref - The remote ref that contains the patch.
    self.ref = patch_dict['currentPatchSet']['ref']
    # revision - The CL's SHA1 hash.
    self.revision = patch_dict['currentPatchSet']['revision']
    self.patch_number = patch_dict['currentPatchSet']['number']
    self.commit = patch_dict['currentPatchSet']['revision']
    self.owner, _, _ = patch_dict['owner']['email'].partition('@')
    self.gerrit_number = patch_dict['number']
    self.url = patch_dict['url']
    # status - Current state of this change.  Can be one of
    # ['NEW', 'SUBMITTED', 'MERGED', 'ABANDONED'].
    self.status = patch_dict['status']
    # Allows a caller to specify why we can't apply this change when we
    # HandleApplicaiton failures.
    self.apply_error_message = ('Please re-sync, rebase, and re-upload your '
                                'change.')

  def __getnewargs__(self):
    """Used for pickling to re-create patch object."""
    return self.patch_dict, self.internal

  def IsAlreadyMerged(self):
    """Returns whether the patch has already been merged in Gerrit."""
    return self.status == 'MERGED'

  def _GetProjectUrl(self):
    """Returns the url to the gerrit project."""
    if self.internal:
      url_prefix = constants.GERRIT_INT_SSH_URL
    else:
      url_prefix = self._PUBLIC_URL

    return os.path.join(url_prefix, self.project)

  def _RebaseOnto(self, branch, upstream, project_dir, trivial):
    """Attempts to rebase FETCH_HEAD onto branch -- while not on a branch.

    Raises:
      cros_lib.RunCommandError:  If the rebase operation returns an error code.
        In this case, we still rebase --abort before returning.
    """
    try:
      git_rb = ['git', 'rebase']
      if trivial: git_rb.extend(['--strategy', 'resolve', '-X', 'trivial'])
      git_rb.extend(['--onto', branch, upstream, 'FETCH_HEAD'])
      # Run the rebase command.
      cros_lib.RunCommand(git_rb, cwd=project_dir, print_cmd=False)

    except cros_lib.RunCommandError:
      cros_lib.RunCommand(['git', 'rebase', '--abort'], cwd=project_dir,
                          error_ok=True, print_cmd=False)
      raise

  def _RebasePatch(self, buildroot, project_dir, trivial):
    """Rebase patch fetched from gerrit onto constants.PATCH_BRANCH.

    When the function completes, the constants.PATCH_BRANCH branch will be
    pointing to the rebased change.

    Arguments:
      buildroot: The buildroot.
      project_dir: Directory of the project that is being patched.
      trivial: Use trivial logic that only allows trivial merges.  Note:
        Requires Git >= 1.7.6 -- bug <.  Bots have 1.7.6 installed.

    Raises:
      ApplyPatchException: If the patch failed to apply.
    """
    url = self._GetProjectUrl()
    upstream = _GetProjectManifestBranch(buildroot, self.project)
    cros_lib.RunCommand(['git', 'fetch', url, self.ref], cwd=project_dir,
                        print_cmd=False)
    try:
      self._RebaseOnto(constants.PATCH_BRANCH, upstream, project_dir, trivial)
      cros_lib.RunCommand(['git', 'checkout', '-B', constants.PATCH_BRANCH],
                          cwd=project_dir, print_cmd=False)
    except cros_lib.RunCommandError:
      try:
        # Failed to rebase against branch, try TOT.
        self._RebaseOnto(upstream, upstream, project_dir, trivial)
      except cros_lib.RunCommandError:
        raise ApplyPatchException(
            self, patch_type=ApplyPatchException.TYPE_REBASE_TO_TOT)
      else:
        # We failed to apply to patch_branch but succeeded against TOT.
        # We should pass a different type of exception in this case.
        raise ApplyPatchException(
            self, patch_type=ApplyPatchException.TYPE_REBASE_TO_PATCH_INFLIGHT)

    finally:
      cros_lib.RunCommand(['git', 'checkout', constants.PATCH_BRANCH],
                          cwd=project_dir, print_cmd=False)

  def Apply(self, buildroot, trivial=False):
    """Implementation of Patch.Apply().

    Raises:
      ApplyPatchException: If the patch failed to apply.
    """
    logging.info('Attempting to apply change %s', self)
    project_dir = self.ProjectDir(buildroot)
    if not cros_lib.DoesLocalBranchExist(project_dir, constants.PATCH_BRANCH):
      upstream = cros_lib.GetManifestDefaultBranch(buildroot)
      cros_lib.RunCommand(['git', 'checkout', '-b', constants.PATCH_BRANCH,
                           '-t', 'm/' + upstream], cwd=project_dir,
                          print_cmd=False)
    self._RebasePatch(buildroot, project_dir, trivial)

  def CommitMessage(self, buildroot):
    """Returns the commit message for the patch as a string."""
    url = self._GetProjectUrl()
    project_dir = self.ProjectDir(buildroot)
    cros_lib.RunCommand(['git', 'fetch', url, self.ref], cwd=project_dir,
                        print_cmd=False)
    return_obj = cros_lib.RunCommand(['git', 'show', '-s', 'FETCH_HEAD'],
                                     cwd=project_dir, redirect_stdout=True,
                                     print_cmd=False)
    return return_obj.output

  def GerritDependencies(self, buildroot):
    """Returns an ordered list of dependencies from Gerrit.

    The list of changes are in order from FETCH_HEAD back to m/master.

    Arguments:
      buildroot: The buildroot.
    Returns:
      An ordered list of Gerrit revisions that this patch depends on.
    Raises:
      MissingChangeIDException: If a dependent change is missing its ChangeID.
    """
    dependencies = []
    url = self._GetProjectUrl()
    logging.info('Checking for Gerrit dependencies for change %s', self)
    project_dir = self.ProjectDir(buildroot)
    cros_lib.RunCommand(['git', 'fetch', url, self.ref], cwd=project_dir,
                        print_cmd=False)
    return_obj = cros_lib.RunCommand(
        ['git', 'log', '-z', '%s..FETCH_HEAD^' %
          _GetProjectManifestBranch(buildroot, self.project)],
        cwd=project_dir, redirect_stdout=True, print_cmd=False)

    for patch_output in return_obj.output.split('\0'):
      if not patch_output: continue
      change_id_match = self._GIT_CHANGE_ID_RE.search(patch_output)
      if change_id_match:
        dependencies.append(change_id_match.group(1))
      else:
        raise MissingChangeIDException('Missing Change-Id in %s' % patch_output)

    if dependencies:
      logging.info('Found %s Gerrit dependencies for change %s', dependencies,
                   self)

    return dependencies

  def PaladinDependencies(self, buildroot):
    """Returns an ordered list of dependencies based on the Commit Message.

    Parses the Commit message for this change looking for lines that follow
    the format:

    CQ-DEPEND:change_num+ e.g.

    A commit which depends on a couple others.

    BUG=blah
    TEST=blah
    CQ-DEPEND=10001,10002
    """
    dependencies = []
    logging.info('Checking for CQ-DEPEND dependencies for change %s', self)
    commit_message = self.CommitMessage(buildroot)
    matches = self._PALADIN_DEPENDENCY_RE.findall(commit_message)
    for match in matches:
      dependencies.extend(self._PALADIN_BUG_RE.findall(match))

    if dependencies:
      logging.info('Found %s Paladin dependencies for change %s', dependencies,
                   self)
    return dependencies

  def __str__(self):
    """Returns custom string to identify this patch."""
    return '%s:%s' % (self.owner, self.gerrit_number)

  # Define methods to use patches in sets.  We uniquely identify patches
  # by Gerrit change numbers.
  def __hash__(self):
    return hash(self.id)

  def __eq__(self, other):
    return self.id == other.id


def RemovePatchRoot(patch_root):
  """Removes the temporary directory storing patches."""
  assert os.path.basename(patch_root).startswith(_TRYBOT_TEMP_PREFIX)
  shutil.rmtree(patch_root)


class LocalPatch(Patch):
  """Object that represents a set of local commits that will be patched."""

  def __init__(self, project, tracking_branch, patch_dir, local_branch):
    """Construct a LocalPatch object.

    Args:
      project: Same as Patch constructor arg.
      tracking_branch: Same as Patch constructor arg.
      patch_dir: The directory where the .patch files are stored.
      local_branch:  The local branch of the project that the patch came from.
    """
    Patch.__init__(self, project, tracking_branch)
    self.patch_dir = patch_dir
    self.local_branch = local_branch

  def _GetFileList(self):
    """Return a list of .patch files in sorted order."""
    file_list = glob.glob(os.path.join(self.patch_dir, '*'))
    file_list.sort()
    return file_list

  def Apply(self, buildroot, trivial=False):
    """Implementation of Patch.Apply().  Does not accept trivial option.

    Raises:
      PatchException if the patch is for the wrong tracking branch.
    """
    assert not trivial, 'Local apply not compatible with trivial set'
    manifest_branch = _GetProjectManifestBranch(buildroot, self.project)
    if self.tracking_branch != manifest_branch:
      raise PatchException('branch %s for project %s is not tracking %s'
                           % (self.local_branch, self.project,
                              manifest_branch))

    project_dir = self.ProjectDir(buildroot)
    try:
      cros_lib.RunCommand(['repo', 'start', constants.PATCH_BRANCH, '.'],
                          cwd=project_dir)
      cros_lib.RunCommand(['git', 'am', '--3way'] + self._GetFileList(),
                          cwd=project_dir)
    except cros_lib.RunCommandError:
      raise ApplyPatchException(self)

  def __str__(self):
    """Returns custom string to identify this patch."""
    return '%s:%s' % (self.project, self.local_branch)


def _GetRemoteTrackingBranch(project_dir, branch):
  """Get the remote tracking branch of a local branch.

  Raises:
    cros_lib.NoTrackingBranchException if branch does not track anything.
  """
  (remote, ref) = cros_lib.GetTrackingBranch(branch, project_dir)
  return cros_lib.GetShortBranchName(remote, ref)


def _GetProjectManifestBranch(buildroot, project):
  """Get the branch specified in the manifest for the project."""
  (remote, ref) = cros_lib.GetProjectManifestBranch(buildroot,
                                                    project)
  return cros_lib.GetShortBranchName(remote, ref)


def PrepareLocalPatches(patches, manifest_branch):
  """Finish validation of parameters, and save patches to a temp folder.

  Args:
    patches:  A list of user-specified patches, in project[:branch] form.
    manifest_branch: The manifest branch of the buildroot.

  Raises:
    PatchException if:
      1. The project branch isn't specified and the project isn't on a branch.
      2. The project branch doesn't track a remote branch.
  """
  patch_info = []
  patch_root = tempfile.mkdtemp(prefix=_TRYBOT_TEMP_PREFIX)

  for patch_id in range(0, len(patches)):
    project, branch = patches[patch_id].split(':')
    project_dir = cros_lib.GetProjectDir('.', project)

    patch_dir = os.path.join(patch_root, str(patch_id))
    cmd = ['git', 'format-patch', '%s..%s' % ('m/' + manifest_branch, branch),
           '-o', patch_dir]
    cros_lib.RunCommand(cmd, redirect_stdout=True, cwd=project_dir)
    if not os.listdir(patch_dir):
      raise PatchException('No changes found in %s:%s' % (project, branch))

    # Store remote tracking branch for verification during patch stage.
    try:
      tracking_branch = _GetRemoteTrackingBranch(project_dir, branch)
    except cros_lib.NoTrackingBranchException:
      raise PatchException('%s:%s needs to track a remote branch!'
                           % (project, branch))

    patch_info.append(LocalPatch(project, tracking_branch, patch_dir, branch))

  return patch_info
