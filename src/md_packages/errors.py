"""Domain errors raised while reading and resolving package scopes."""


class MdPackageError(Exception):
    """Base class for expected, user-actionable package errors."""


class ManifestError(MdPackageError):
    """An authored manifest is invalid."""


class ResolutionError(MdPackageError):
    """The effective package graph cannot be resolved."""


class ConflictError(ResolutionError):
    """Two declarations claim incompatible identities or targets."""


class PathValidationError(ManifestError):
    """A package-relative target is unsafe or unsupported."""


class SourceSymlinkError(ManifestError):
    """A distributable source contains a symbolic link."""
