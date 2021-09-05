"""
Functions related to Total Electron Content calculations.
Basically all the weird combinations of signals used in
all the GNSS textbooks and ionospheric calculations
belong in here
"""
from __future__ import annotations  # defer type annotations due to circular stuff

from typing import TYPE_CHECKING, Optional
import numpy

from laika import constants

from tid import util


# deal with circular type definitions for Scenario
if TYPE_CHECKING:
    from tid.scenario import Connection, Scenario

K = 40.308e16
M_TO_TEC = 6.158  # meters of L1 error to TEC
# set ionosphere puncture to 350km
IONOSPHERE_H = constants.EARTH_RADIUS + 350000
# maximum density of electrons for slant calculation
IONOSPHERE_MAX_D = constants.EARTH_RADIUS + 350000
C = constants.SPEED_OF_LIGHT


def melbourne_wubbena(
    scn: Scenario, observations: numpy.array
) -> Optional[numpy.array]:
    """
    Calculate the Melbourne Wubbena signal combination for these observations.
    This relies on being able to get the frequencies (which can sometimes fail for
    GLONASS which uses FDMA)

    Args:
        frequencies: chan1 and chan2 frequencies
        observations: our dense data format

    Returns:
        numpy array of MW values or None if the calculation couldn't be completed
    """
    # calculate Melbourne Wubbena, this should be relatively constant
    # during a single connection
    frequencies = scn.get_frequencies(observations)
    # if we can't get this, we won't be able to do our other calculations anyway
    if frequencies is None:
        return None
    f1, f2 = frequencies

    try:
        chan2 = util.channel2(observations)
    except LookupError:
        # if there's no chan 2 data, we're done
        return None

    phase = C / (f1 - f2) * (observations["L1C"] - observations["L2C"])
    pseudorange = 1 / (f1 + f2) * (f1 * observations["C1C"] + f2 * observations[chan2])
    # wavelength = C/(f0 - f2)
    return phase - pseudorange


def calc_delay_factor(connection: Connection) -> float:
    """
    Calculate the delay factor: the strength of the ionospheric delay that the
    signal experiences, due to the frequency

    Args:
        connection: the connection of interest

    Returns:
        delay factor, in units of seconds^2
    """
    f1, f2 = connection.frequencies
    return ((f1 ** 2) * (f2 ** 2)) / ((f1 ** 2) - (f2 ** 2))


def calc_carrier_delays(connection: Connection) -> numpy.array:
    """
    Calculate delay differences between L1C and L2C signals
    Normalized to meters

    Args:
        connection: the connection for which we want to calculate the carrier delays

    Returns:
        numpy array of the differences, in units of meters
    """

    sat_bias = connection.scenario.sat_biases.get(connection.prn, 0)
    station_bias = connection.scenario.rcvr_biases.get(connection.station, 0)

    raw_phase_difference_meters = C * (
        connection.observations["L1C"] - connection.observations["L2C"]
    )
    return (
        raw_phase_difference_meters
        + connection.carrier_correction_meters
        + sat_bias
        - station_bias
    )


def s_to_v_factor(elevations: numpy.array, ionh: float = IONOSPHERE_H):
    """
    Calculate the unitless scaling factor to translate the slant ionospheric measurement
    to the vertical value.

    Args:
        elevations: a list of elevations (in units of radians)
        ionh: optional radius of the ionosphere in meters where the pierce point occurs

    Returns:
        numpy array of the unitless scaling factors
    """
    return numpy.sqrt(1 - (numpy.cos(elevations) * constants.EARTH_RADIUS / ionh) ** 2)


def ion_locs(
    rec_pos: numpy.array, sat_pos: numpy.array, ionh: float = IONOSPHERE_H
) -> numpy.array:
    """
    Given a receiver and a satellite, where does the line between them intersect
    with the ionosphere?

    Based on:
    http://www.ambrsoft.com/TrigoCalc/Sphere/SpherLineIntersection_.htm

    All positions are XYZ ECEF values in meters

    Args:
        rec_pos: the receiver position(s), a numpy array of shape (?,3)
        sat_pos: the satellite position(s), a numpy array of shape (?,3), same length as rec_pos
        ionh: optional radius of the ionosphere in meters

    Returns:
        numpy array of positions of ionospheric pierce points
    """
    a = numpy.sum((sat_pos - rec_pos) ** 2, axis=1)
    b = 2 * numpy.sum((sat_pos - rec_pos) * rec_pos, axis=1)
    c = numpy.sum(rec_pos ** 2) - ionh ** 2

    common = numpy.sqrt(b ** 2 - (4 * a * c)) / (2 * a)
    b_scaled = -b / (2 * a)
    # TODO, I think the there is a clever way to vectorize the loop below
    # solutions = numpy.stack((b_scaled + common, b_scaled - common), axis=1)

    # for each solution, use the one with the smallest absolute value
    # (that is the closest intersection, the other is the further intersection)
    scale = numpy.zeros(sat_pos.shape)
    for i, (x, y) in enumerate(zip(b_scaled + common, b_scaled - common)):
        if abs(x) < abs(y):
            smallest = x
        else:
            smallest = y
        scale[i][0] = smallest
        scale[i][1] = smallest
        scale[i][2] = smallest

    return rec_pos + (sat_pos - rec_pos) * scale
