import argparse
from datetime import datetime, timedelta
from laika import AstroDog
import logging

from pytid.utils.configuration import Configuration
from pytid.gnss import bias_solve, connections, get_data, plot

conf = Configuration()

dog = AstroDog(cache_dir=conf.gnss.get("cache_dir"))


def collect_and_plot(start_date: datetime, duration: timedelta, logger: logging.Logger):
    stations = conf.gnss.get("stations")

    # ask Laika for station location data + GNSS data from our stations
    logger.info("Gathering GNSS data from stations...")
    station_locs, station_data = get_data.populate_data(dog, start_date, duration, stations)

    # turn our station data into "connections" which are periods of
    # signal lock without cycle slips
    logger.info("Reorganizing data into connections")
    conns = connections.get_connections(dog, station_locs, station_data)
    # then organize it so it's easier to look up
    print("organising")
    conn_map = connections.make_conn_map(conns)

    # attempt to solve integer ambiguities
    logger.info("Solving ambiguities")
    connections.correct_conns(station_locs, station_data, conns)

    # this will get vtec data, accounting for ambiguities but NOT clock biases
    logger.info("Calculating vTEC data")
    station_vtecs = get_data.get_vtec_data(dog, station_locs, station_data, conn_map=conn_map)

    # this attempts to find coincidences of satellites and station observations
    # from which to derive biases
    logger.info("Locating coincidences for biases")
    cal_dat = bias_solve.gather_data(station_vtecs)

    # this uses least squares to attempt to resolve said ambiguities
    # XXX: you want quite a few datapoints for this to work well
    logger.info("Resolving biases")
    sat_biases, rcvr_biases, tecs = bias_solve.lsq_solve(*cal_dat)

    # now go back and update our vtecs data...
    logger.info("Correcting vTEC data with biases")
    corrected_vtecs = get_data.correct_vtec_data(station_vtecs, sat_biases, rcvr_biases)

    logger.info("Plotting data")
    plotter = plot.StationPlotter(vtecs=corrected_vtecs, date=start_date)
    for station in stations:
        plotter.plot_station(station)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", "-d", help="The start date for the plot interval (YYYY-mm-dd)")
    parser.add_argument("--duration-days", "-t", help="The duration in days for the plot", default=1)
    parser.add_argument("--log", "-l", help="set the logging level", type=str, default="INFO")

    args = parser.parse_args()
    if args.start_date is None:
        print(parser.format_help())

    start_date = datetime.strptime(args.start_date, "%Y-%m-%d")
    duration = timedelta(days=args.duration_days)

    numeric_level = getattr(logging, args.log.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Invalid log level: {args.log}")

    logger = logging.getLogger(__name__)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(filename)s:%(lineno)d][%(levelname)s] %(message)s",
        datefmt=conf.logging.get("datefmt")
    )

    collect_and_plot(start_date, duration, logger)