"""
By default, Laika's objects are not very memory efficient
This file has wrappers/helpers/definitions for a more
space efficient format based on numpy
"""

from typing import cast, Dict, List, Sequence
import numpy

from laika import AstroDog
from laika.gps_time import GPSTime
from laika.raw_gnss import GNSSMeasurement

from tid import get_data, types

# dict of prn -> numpy.array(dtype=DENSE_TYPE)
DenseMeasurements = Dict[str, types.DenseDataType]

DENSE_TYPE = [
    #    ("station", "U4"),  # name of the ground station
    #    ("prn", "U3"),  # name of the GNSS satellite (one letter then 2 digit number)
    ("tick", "i4"),  # tick number the observation was made
    ("C1C", "f8"),  # GNSS measurements, if available
    ("C2C", "f8"),
    ("C2P", "f8"),
    ("C5C", "f8"),
    ("L1C", "f8"),
    ("L2C", "f8"),
    ("L5C", "f8"),
    # second and week, allowing reconstruction of GPS timestamp
    ("recv_time_sec", "f4"),
    ("recv_time_week", "i4"),
    ("sat_clock_err", "f8"),  # clock error in seconds
    # TODO: this is the same for every station, can we store once and just keep final pos?
    ("sat_pos", "3f8"),  # satellite position XYZ ECEF in meters
    ("sat_vel", "3f8"),  # satellite velocity XYZ ECEF in meters/second
    # satellite position in XYZ ECEF in meters after corrections
    ("sat_pos_final", "3f8"),
    ("is_processed", "?"),  # boolean for initial processing
    ("is_corrected", "?"),  # boolean for final positions being calculated
]


def _meas_to_tuple(raw_meas: GNSSMeasurement) -> tuple:
    """
    Given a raw Laika GNSSMeasurement into a tuple to be used by our numpy struct

    Args:
        raw_meas: the Laika measurement

    Returns:
        tuple of the data values in question
    """
    return (
        #        station_name,
        #        raw_meas.prn,
        0,
        raw_meas.observables.get("C1C", numpy.nan),
        raw_meas.observables.get("C2C", numpy.nan),
        raw_meas.observables.get("C2P", numpy.nan),
        raw_meas.observables.get("C5C", numpy.nan),
        raw_meas.observables.get("L1C", numpy.nan),
        raw_meas.observables.get("L2C", numpy.nan),
        raw_meas.observables.get("L5C", numpy.nan),
        raw_meas.recv_time_sec,
        raw_meas.recv_time_week,
        raw_meas.sat_clock_err,
        (
            raw_meas.sat_pos[0].item(),
            raw_meas.sat_pos[1].item(),
            raw_meas.sat_pos[2].item(),
        ),
        (
            raw_meas.sat_vel[0].item(),
            raw_meas.sat_vel[1].item(),
            raw_meas.sat_vel[2].item(),
        ),
        (
            raw_meas.sat_pos_final[0].item(),
            raw_meas.sat_pos_final[1].item(),
            raw_meas.sat_pos_final[2].item(),
        ),
        bool(raw_meas.corrected),
        bool(raw_meas.processed),
    )


def from_raw_obs(raw_obs: Sequence[Sequence[GNSSMeasurement]]) -> DenseMeasurements:
    """
    Given a raw observation from a station, convert it to the more memory efficient format

    Args:
        station_name: string representing the name of the station
        raw_obs: the data that Laika returns from raw_gnss.read_rinex_obs

    Returns:
        a dictionary mapping station names to numpy arrays of our "dense measurements"
    """
    sv_dict_tmp: Dict[str, List] = {}
    sv_dict_out: Dict[str, types.DenseDataType] = {}

    # use python lists to build up data struct, because they're faster to modify
    for sat_obs in raw_obs:
        for obs in sat_obs:
            if obs.prn not in sv_dict_tmp:
                sv_dict_tmp[obs.prn] = []
            sv_dict_tmp[obs.prn].append(_meas_to_tuple(obs))

    # convert the python lists into numpy arrays to save a bit of memory
    for key, value in sv_dict_tmp.items():
        sv_dict_out[key] = cast(
            types.DenseDataType, numpy.array(value, dtype=DENSE_TYPE)
        )
        if numpy.all(numpy.isnan(sv_dict_out[key]["C2C"])):
            sv_dict_out[key][["C2C", "C2P"]] = sv_dict_out[key][["C2P", "C2C"]]

    return sv_dict_out


def dense_data_for_station(
    dog: AstroDog, time: GPSTime, station_name: str
) -> DenseMeasurements:
    """
    Get data from a particular station and time. Wrapper for data_for_station
    inside of get_data

    Args:
        dog: laika AstroDog object
        time: laika GPSTime object for the time in question

    Returns:
        a tuple consisting of
            aproximate x,y,z location in ECEF meters
            raw_rinex data

    Raises:
        DownloadError if the data could not be fetched

    TODO: caching of the results on disk? or should that happen later?
    """
    return from_raw_obs(get_data.data_for_station(dog, time, station_name))


def merge_data(data1: DenseMeasurements, data2: DenseMeasurements) -> DenseMeasurements:
    """
    Merges two sets of dense measurements together

    Args:
        data1: the first (chronologically) set of data
        data2: the second (chronologically) set of data

    Returns:
        the combined data
    """
    combined = data1.copy()
    for prn in data2:
        # prn only has data in the second dataset
        if prn not in data1:
            combined[prn] = data2[prn]
        # otherwise we need an actual merge
        else:
            combined[prn] = numpy.append(data1[prn], data2[prn])

    return combined
