"""Module to represent BTicino modules."""
from __future__ import annotations

import logging

from ..modules.module import BoilerMixin, FirmwareMixin, HumidityMixin, Module, Switch

LOG = logging.getLogger(__name__)


class BNDL(Module):
    """BTicino door lock."""


class BNSL(Switch):  # pylint: disable=too-many-ancestors
    """BTicino staircase light."""


class BNCX(Module):
    """BTicino internal panel = gateway."""


class BNEU(Module):
    """BTicino external unit."""


class BNCS(Module):
    """BTicino camera."""


class BNXM(Module):
    """BTicino X meter."""


class BNMS(Module):
    """BTicino motorized shade."""


class BNAS(Module):
    """BTicino automatic shutter."""


class BNAB(Module):
    """BTicino automatic blind."""


class BNMH(Module):
    """BTicino automatic blind."""


class BNTH(FirmwareMixin, BoilerMixin, HumidityMixin, Module):
    """BTicino thermostat."""


class BNFC(Module):
    """BTicino fan coil."""


class BNTR(Module):
    """BTicino radiator thermostat."""
