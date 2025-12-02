from aws_cdk import (
    aws_route53 as route53,
    Duration,
    RemovalPolicy,
)
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig


class DNSRecord:
    """DNS record to be created in the hosted zone"""

    def __init__(
        self,
        name: str,
        record_type: str,
        values: list[str],
        ttl: int | None = None,
        comment: str | None = None,
    ):
        self.name = name
        self.record_type = record_type.upper()
        self.values = values
        self.ttl = ttl or 300  # Default TTL of 5 minutes
        self.comment = comment


class HostedZoneConstruct(Construct):
    """Route53 hosted zone with configurable DNS records"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: EnvironmentConfig,
        zone_name: str | None = None,
        dns_records: list[DNSRecord] | None = None,
        create_ns_records: bool = True,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config
        self._zone_name = zone_name or config.domain_name
        self._dns_records = dns_records or []
        self._create_ns_records = create_ns_records

        # Create hosted zone
        self.hosted_zone = self._create_hosted_zone()

        # Create DNS records
        self.records = self._create_dns_records()

    def _create_hosted_zone(self) -> route53.IHostedZone:
        """Create or import existing Route53 hosted zone"""
        # First, try to look up existing hosted zone
        existing_zone = route53.HostedZone.from_lookup(
            self,
            "ExistingHostedZone",
            domain_name=self._zone_name,
        )

        # Check if zone was found (from_lookup returns a valid zone even if not found)
        # We'll create a new one if lookup fails
        try:
            # Try to access the zone_id to verify it exists
            _ = existing_zone.hosted_zone_id
            return existing_zone
        except Exception:
            # Zone doesn't exist, create a new one
            new_zone = route53.HostedZone(
                self,
                "HostedZone",
                zone_name=self._zone_name,
                comment=f"Hosted zone for {self._zone_name} - {self.config.environment} environment",
            )
            # Set removal policy to RETAIN so zone isn't deleted on stack destroy
            new_zone.apply_removal_policy(RemovalPolicy.RETAIN)
            return new_zone

    def _create_dns_records(self) -> list[route53.RecordSet]:
        """Create DNS records in the hosted zone"""
        records = []

        for dns_record in self._dns_records:
            record_name = dns_record.name
            # If record name doesn't end with zone name, make it relative to zone
            if not record_name.endswith(self._zone_name):
                if record_name == "@" or record_name == "":
                    record_name = self._zone_name
                else:
                    record_name = f"{record_name}.{self._zone_name}"

            # Create the appropriate record type
            if dns_record.record_type == "A":
                record = route53.ARecord(
                    self,
                    f"ARecord-{dns_record.name}",
                    zone=self.hosted_zone,
                    record_name=record_name,
                    target=route53.RecordTarget.from_ip_addresses(*dns_record.values),
                    ttl=Duration.seconds(dns_record.ttl),
                    comment=dns_record.comment,
                )
            elif dns_record.record_type == "AAAA":
                record = route53.AaaaRecord(
                    self,
                    f"AAAARecord-{dns_record.name}",
                    zone=self.hosted_zone,
                    record_name=record_name,
                    target=route53.RecordTarget.from_ip_addresses(*dns_record.values),
                    ttl=Duration.seconds(dns_record.ttl),
                    comment=dns_record.comment,
                )
            elif dns_record.record_type == "CNAME":
                record = route53.CnameRecord(
                    self,
                    f"CNAMERecord-{dns_record.name}",
                    zone=self.hosted_zone,
                    record_name=record_name,
                    domain_name=dns_record.values[0],  # CNAME only has one value
                    ttl=Duration.seconds(dns_record.ttl),
                    comment=dns_record.comment,
                )
            elif dns_record.record_type == "MX":
                mx_values = []
                for value in dns_record.values:
                    # Expected format: "priority mailserver.example.com"
                    parts = value.split(" ", 1)
                    if len(parts) == 2:
                        priority = int(parts[0])
                        hostname = parts[1]
                        mx_values.append(
                            route53.MxRecordValue(priority=priority, host_name=hostname)
                        )

                record = route53.MxRecord(
                    self,
                    f"MXRecord-{dns_record.name}",
                    zone=self.hosted_zone,
                    record_name=record_name,
                    values=mx_values,
                    ttl=Duration.seconds(dns_record.ttl),
                    comment=dns_record.comment,
                )
            elif dns_record.record_type == "TXT":
                record = route53.TxtRecord(
                    self,
                    f"TXTRecord-{dns_record.name}",
                    zone=self.hosted_zone,
                    record_name=record_name,
                    values=dns_record.values,
                    ttl=Duration.seconds(dns_record.ttl),
                    comment=dns_record.comment,
                )
            else:
                # Generic record for other types
                record = route53.RecordSet(
                    self,
                    f"{dns_record.record_type}Record-{dns_record.name}",
                    zone=self.hosted_zone,
                    record_name=record_name,
                    record_type=getattr(route53.RecordType, dns_record.record_type),
                    target=route53.RecordTarget.from_values(*dns_record.values),
                    ttl=Duration.seconds(dns_record.ttl),
                    comment=dns_record.comment,
                )

            records.append(record)

        return records

    def add_record(self, dns_record: DNSRecord) -> route53.RecordSet:
        """Add a single DNS record to the hosted zone"""
        self._dns_records.append(dns_record)
        # Create and return the new record
        return self._create_dns_records()[-1]

    @property
    def zone_id(self) -> str:
        """Get hosted zone ID"""
        return self.hosted_zone.hosted_zone_id

    @property
    def zone_name(self) -> str:
        """Get hosted zone name"""
        return self.hosted_zone.zone_name

    @property
    def name_servers(self) -> list[str]:
        """Get name servers for the hosted zone"""
        return self.hosted_zone.hosted_zone_name_servers or []

    @property
    def zone_arn(self) -> str:
        """Get hosted zone ARN"""
        return self.hosted_zone.hosted_zone_arn
