"""
Connections are periods of continuous lock (and therefore carrier phase offsets)
between satellites and ground stations.
Things to manage those are stored here
"""
from __future__ import annotations  # defer type annotations due to circular stuff
from functools import cached_property
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Iterable,
    Optional,
    Sequence,
    Tuple,
    Union,
)

import numpy

from laika.lib import coordinates

from tid import tec, types, util

# deal with circular type definitions for Scenario
if TYPE_CHECKING:
    from tid.scenario import Scenario


class Connection:
    """
    Each time a receiver acquires a lock on a GNSS satellite,
    some random error of an unknown wavelengths are accumulated
    in the phase measurements. A period of continuous lock
    is referred to as a "connection"

    Therefore, each connection needs to be established so that
    we can solve for the unknown wavelength difference.

    The longer the connection, the more data and better job
    we can do. However, if we mess up, we can introduce extra
    noise.
    """

    def __init__(
        self,
        scenario: Scenario,
        station: str,
        prn: str,
        idx_start: int,
        idx_end: int,
    ) -> None:
        """
        Args:
            scenario: scenario to which this connection belongs
            station: station name
            prn: satellite svid
            idx_start: first data index in scenario data
            idx_end: last data index in scenario data
            filter_ticks: whether we should run processing to determine
                how long this connection lasts, possibly truncating it
        """
        self.scenario = scenario

        self.station = station
        self.prn = prn
        self.idx_start = idx_start
        self.idx_end = idx_end

        self.tick_start = scenario.station_data[station][prn][idx_start]["tick"]
        self.tick_end = scenario.station_data[station][prn][idx_end]["tick"]

        self.missing_ticks = set()
        self._init_missing_ticks()

        # integer ambiguities, the phase correction information
        # that is the goal of this whole connections stuff
        self.n_chan1 = None
        self.n_chan2 = None

        # "raw" offset: the difference from the code phase tec values
        self.offset = None  # this value has units of Meters
        self.offset_error = None

    def _init_missing_ticks(self) -> None:
        """
        Fill out missing ticks table, used when trying to query some data
        from connections
        """
        gap_idxs = numpy.where(numpy.diff(self.observations["tick"]) > 1)[0]
        for gap_idx in gap_idxs:
            first, last = self.observations["tick"][gap_idx : gap_idx + 2]
            self.missing_ticks |= set(range(first + 1, last))

    def tick_idx(self, tick) -> Optional[int]:
        """
        Because we might miss ticks in our observations, this has a helpful mapping
        from tick to idx in this data struct.

        Args:
            tick: the tick number to look up, must be in [tick_start, tick_end]

        Returns:
            idx where that tick is found, or None if it doesn't exist
        """
        if tick in self.missing_ticks:
            return None
        guess = tick - self.tick_start
        for missing in self.missing_ticks:
            if tick > missing:
                guess -= 1
        return guess

    @property
    def is_glonass(self) -> bool:
        """
        Is this a GLONASS satellite?

        Returns:
            boolean indicating glonass or not
        """
        return self.prn.startswith("R")

    @cached_property
    def glonass_chan(self) -> int:
        """
        The channel that GLONASS is using.

        Returns:
            the integer channel GLONASS is using, or 0 if it is not using GLONASS
        """
        if not self.is_glonass:
            return 0
        chan = self.scenario.get_glonass_chan(self.prn, self.observations)
        # can't have gotten None, or we'd not have gotten it in our connection
        assert chan is not None
        return chan

    @cached_property
    def frequencies(self) -> Tuple[float, float]:
        """
        The frequencies that correspond to this connection
        """
        frequencies = self.scenario.get_frequencies(self.prn, self.observations)
        assert frequencies is not None, "Unknown frequencies INSIDE connection object"
        return frequencies

    @property
    def ticks(self) -> numpy.ndarray:
        """
        Numpy array of ticks from tick_start to tick_end (inclusive), for convenience
        """
        return numpy.arange(self.tick_start, self.tick_end + 1)

    @property
    def observations(self) -> types.DenseDataType:
        """
        Convenience function: returns the numpy arrays for the raw observations
        corresponding to this connection
        """
        # note: don't use self.ticks, `range` vs `slice` is a lot slower
        assert self.scenario.station_data
        return self.scenario.station_data[self.station][self.prn][
            self.idx_start : self.idx_end + 1
        ]

    def elevation(
        self, sat_pos: Union[types.ECEF_XYZ, types.ECEF_XYZ_LIST]
    ) -> Union[types.ECEF_XYZ, types.ECEF_XYZ_LIST]:
        """
        Convenience wrapper around scenario.station_el, but specifically
        for the station that this connection uses.

        sat_pos: numpy array of XYZ ECEF satellite positions in meters
            must have shape (?, 3)

        Returns:
            elevation in radians (will have same length as sat_pos)
        """
        return self.scenario.station_el(self.station, sat_pos)

    def __contains__(self, tick: int) -> bool:
        """
        Convenience function: `tick in connection` is true iff tick is in
        the range [self.tick_start, self.tick_end]

        Args:
            tick: the tick to check

        Returns:
            boolean whether or not the tick is included
        """
        return self.tick_start <= tick <= self.tick_end

    def _correct_ambiguities_avg(self) -> None:
        """
        Code phase smoothing for carrier phase offsets

        This is the simplest method: use the average difference between
        the code and carrier phases.
        """

        f1, f2 = self.frequencies
        # sign reversal here is correct: ionospheric effect is opposite for code phase
        code_phase_diffs = self.observations["C2C"] - self.observations["C1C"]
        carrier_phase_diffs = tec.C * (
            self.observations["L1C"] / f1 - self.observations["L2C"] / f2
        )
        difference = code_phase_diffs - carrier_phase_diffs
        # assert abs(numpy.mean(difference)) < 100
        self.offset = numpy.mean(difference)
        self.offset_error = numpy.std(difference)

    def correct_ambiguities(self) -> None:
        """
        Attempt to calculate the offsets from L1C to L2C
        In the complex case by using integer ambiguities
        In the simple case by code phase smoothing
        """
        self._correct_ambiguities_avg()

    @property
    def carrier_correction_meters(self) -> float:
        """
        Returns the correction factor for the chan1 chan2 difference
        This may be calculated with integer ambiguity corrections, or
        using code-phase smoothing

        Note: This could be cached, but this calculation is too simple to be worth it
        """
        # if we have integer ambiguity data, use that
        if self.n_chan1 is not None and self.n_chan2 is not None:
            f1, f2 = self.frequencies
            return tec.C * (self.n_chan2 / f2 - self.n_chan1 / f1)

        # otherwise use the code-phase smoothed difference values
        if self.offset is not None:
            return self.offset

        assert False, "carrier correction attempted with no correction mechanism"

    @property
    def ipps(self) -> types.ECEF_XYZ_LIST:
        """
        The locations where the signals associated with this connection
        penetrate the ionosphere.

        Returns:
            numpy array of XYZ ECEF coordinates in meters of the IPPs
        """
        return tec.ion_locs(
            self.scenario.station_locs[self.station], self.observations["sat_pos"]
        )

    @property
    def vtecs(self) -> numpy.ndarray:
        """
        The vtec values associated with this connection

        Returns:
            numpy array of (
                vtec value in TECu,
                unitless slant_to_vertical factor
            )
        """
        return tec.calculate_vtecs(self)


class SparseList:
    """
    Helper to represent data from connections where we may be missing stuff
    Don't store all those 0s!
    """

    def __init__(
        self,
        index_ranges: Iterable[Tuple[int, int]],
        data: Iterable[Sequence],
        tick_lookup: Iterable[Callable[[int], Optional[int]]],
        default: Any = 0.0,
    ):
        self.ranges = index_ranges
        self.data = data
        self.tick_lookup = tick_lookup
        self.default = default
        self.max = max(i[1] for i in index_ranges)

    def __len__(self) -> int:
        """
        Returns the total number of ticks available to be fetched
        (so this matches a Sequence type)

        Returns:
            the integer length
        """
        return self.max + 1

    def __getitem__(self, tick: int) -> Any:
        """
        Fetch the given tick data

        Args:
            tick: the tick number to fetch

        Returns:
            the data associated with that tick, or the default value if it was not found
        """
        for data_range, datum, tick_lookup in zip(
            self.ranges, self.data, self.tick_lookup
        ):
            if data_range[0] <= tick <= data_range[1]:
                idx = tick_lookup(tick)
                if idx is None:
                    return self.default
                return datum[idx]
        return self.default


class ConnTickMap:
    """
    Simple helper class to efficiently convert a tick number back
    into a connection.
    """

    def __init__(self, connections: Iterable[Connection]) -> None:
        self.connections = connections

    def __getitem__(self, tick: int) -> Connection:
        """
        Get the tick for this tick

        Args:
            tick: the tick to fetch a connection for

        Raises KeyError if tick is not in any of the connections
        """
        for con in self.connections:
            if tick in con:
                return con
        raise KeyError

    def get_vtecs(self) -> Sequence[float]:
        """
        Get vtec data for this set of connections

        Returns:
            SparseList of raw VTEC TECu values, one per tick, 0.0 if unknown
        """
        return SparseList(
            [(con.tick_start, con.tick_end) for con in self.connections],
            [con.vtecs[0] for con in self.connections],
            [con.tick_idx for con in self.connections],
        )

    def get_filtered_vtecs(self) -> Sequence[float]:
        """
        Get bandpass filtered vtec data for this set of connections

        Returns:
            SparseList of 2nd order butterworth bandpass filtered VTEC TECu values,
            one per tick, 0.0 if unknown
        """
        index_ranges = []
        data = []
        tick_lookup = []

        for con in self.connections:
            if con.idx_end - con.idx_start < util.BUTTER_MIN_LENGTH:
                # not enough data to filter
                continue
            index_ranges.append((con.tick_start, con.tick_end))
            data.append(util.bpfilter(con.vtecs[0]))
            tick_lookup.append(con.tick_idx)
        return SparseList(index_ranges, data, tick_lookup)

    def get_ipps(self) -> Sequence[Optional[types.ECEF_XYZ]]:
        """
        Get the ionospheric pierce points for each tick in this set of connections.

        Returns:
            SparseList of (
                ECEF XYZ coordinates in meters, or None if there is no
                data for that tick
            )
        """
        return SparseList(
            [(con.tick_start, con.tick_end) for con in self.connections],
            [con.ipps for con in self.connections],
            [con.tick_idx for con in self.connections],
            default=None,
        )

    def get_ipps_latlon(self) -> Sequence[Optional[Tuple[float, float]]]:
        """
        Get the ionospheric pierce points for each tick in this set of connections.

        Returns:
            SparseList of (
                lat, lon values, or None if there is no data for that tick
            )
        """
        return SparseList(
            [(con.tick_start, con.tick_end) for con in self.connections],
            [coordinates.ecef2geodetic(con.ipps)[..., 0:2] for con in self.connections],
            [con.tick_idx for con in self.connections],
            default=None,
        )
