from sqlalchemy import (
    Column, Integer, BigInteger, Float, Text, Date, DateTime, ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Route(Base):
    __tablename__ = "routes"

    route_id = Column(Text, primary_key=True)
    route_short_name = Column(Text, nullable=False)
    route_long_name = Column(Text, nullable=True)
    route_type = Column(Integer, nullable=False)


class Trip(Base):
    __tablename__ = "trips"

    trip_id = Column(Text, primary_key=True)
    route_id = Column(Text, ForeignKey("routes.route_id"), nullable=False, index=True)
    service_id = Column(Text, nullable=False, index=True)
    direction_id = Column(Integer, nullable=False)
    trip_headsign = Column(Text, nullable=True)


class Stop(Base):
    __tablename__ = "stops"

    stop_id = Column(Text, primary_key=True)
    stop_name = Column(Text, nullable=False)
    stop_lat = Column(Float, nullable=True)
    stop_lon = Column(Float, nullable=True)


class StopTime(Base):
    __tablename__ = "stop_times"

    trip_id = Column(Text, ForeignKey("trips.trip_id"), primary_key=True)
    stop_sequence = Column(Integer, primary_key=True)
    stop_id = Column(Text, ForeignKey("stops.stop_id"), nullable=False, index=True)
    arrival_time = Column(Text, nullable=True)
    departure_time = Column(Text, nullable=True)
    pickup_type = Column(Integer, nullable=True)
    drop_off_type = Column(Integer, nullable=True)


class Calendar(Base):
    __tablename__ = "calendar"

    service_id = Column(Text, primary_key=True)
    monday = Column(Integer, nullable=False)
    tuesday = Column(Integer, nullable=False)
    wednesday = Column(Integer, nullable=False)
    thursday = Column(Integer, nullable=False)
    friday = Column(Integer, nullable=False)
    saturday = Column(Integer, nullable=False)
    sunday = Column(Integer, nullable=False)
    start_date = Column(Text, nullable=False)
    end_date = Column(Text, nullable=False)


class ArrivalRecord(Base):
    __tablename__ = "arrival_records"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    poll_timestamp = Column(TIMESTAMP(timezone=True), nullable=False, index=True)
    trip_id = Column(Text, ForeignKey("trips.trip_id"), nullable=False)
    route_id = Column(Text, ForeignKey("routes.route_id"), nullable=False, index=True)
    direction_id = Column(Integer, nullable=False)
    stop_id = Column(Text, ForeignKey("stops.stop_id"), nullable=False)
    stop_sequence = Column(Integer, nullable=False)
    scheduled_time = Column(TIMESTAMP(timezone=True), nullable=True)
    predicted_time = Column(TIMESTAMP(timezone=True), nullable=True)
    delay_seconds = Column(Integer, nullable=True)
    vehicle_id = Column(Text, nullable=True)

    __table_args__ = ()


class DailyRouteMetric(Base):
    __tablename__ = "daily_route_metrics"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    route_id = Column(Text, nullable=False)
    date = Column(Date, nullable=False)
    total_observations = Column(Integer, nullable=False, default=0)
    early_count = Column(Integer, nullable=False, default=0)
    on_time_count = Column(Integer, nullable=False, default=0)
    late_count = Column(Integer, nullable=False, default=0)
    on_time_percentage = Column(Float, nullable=True)
    avg_delay_seconds = Column(Float, nullable=True)

    __table_args__ = (
        UniqueConstraint("route_id", "date", name="uq_daily_route_date"),
    )


class HourlyRouteMetric(Base):
    __tablename__ = "hourly_route_metrics"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    route_id = Column(Text, nullable=False)
    date = Column(Date, nullable=False)
    hour = Column(Integer, nullable=False)
    total_observations = Column(Integer, nullable=False, default=0)
    early_count = Column(Integer, nullable=False, default=0)
    on_time_count = Column(Integer, nullable=False, default=0)
    late_count = Column(Integer, nullable=False, default=0)
    on_time_percentage = Column(Float, nullable=True)
    avg_delay_seconds = Column(Float, nullable=True)

    __table_args__ = (
        UniqueConstraint("route_id", "date", "hour", name="uq_hourly_route_date_hour"),
    )


class LatestSnapshot(Base):
    __tablename__ = "latest_snapshot"

    route_id = Column(Text, primary_key=True)
    route_name = Column(Text, nullable=True)
    route_type = Column(Integer, nullable=True)
    total_observations = Column(Integer, nullable=False, default=0)
    early_count = Column(Integer, nullable=False, default=0)
    on_time_count = Column(Integer, nullable=False, default=0)
    late_count = Column(Integer, nullable=False, default=0)
    on_time_percentage = Column(Float, nullable=True)
    avg_delay_seconds = Column(Float, nullable=True)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=True)


class StaticFeedMeta(Base):
    __tablename__ = "static_feed_meta"

    id = Column(Integer, primary_key=True, autoincrement=True)
    last_modified = Column(Text, nullable=True)
    checked_at = Column(TIMESTAMP(timezone=True), nullable=False)
