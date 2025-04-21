import aioboto3

from machines.config import app_config


class Route53Service:
    def __init__(self):
        self.session = aioboto3.Session(
            aws_access_key_id=app_config.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=app_config.AWS_SECRET_ACCESS_KEY,
            region_name=app_config.AWS_REGION,
        )
        self._hosted_zones = app_config.AWS_ROUTE53_ZONES
        # TODO: make this configurable, if we fail to get the zone name, we should try the next one
        self._hosted_zone = list(self._hosted_zones.keys())[0]
        self._zone_id = self._hosted_zones[self._hosted_zone]

    def get_cname_domain(self, name: str) -> str:
        return f"{name}.{self._hosted_zone}"

    async def create_cname_record(self, name: str) -> None:
        """Create a CNAME record for a machine.

        Args:
            name: name to create the record for
        """
        print(f"Creating CNAME record for {name}")
        fly_app_name = f"{name}.fly.dev"
        async with self.session.client("route53") as client:  # type: ignore
            await client.change_resource_record_sets(
                HostedZoneId=self._zone_id,
                ChangeBatch={
                    "Changes": [
                        {
                            "Action": "CREATE",
                            "ResourceRecordSet": {
                                "Name": self.get_cname_domain(name),
                                "Type": "CNAME",
                                "TTL": 300,
                                "ResourceRecords": [{"Value": fly_app_name}],
                            },
                        }
                    ]
                },
            )

    async def delete_cname_record(self, name: str) -> None:
        """Delete a CNAME record for a machine.

        Args:
            name: name to delete the record for
        """
        current_record = await self.get_cname_record(name)
        if not current_record:
            return

        async with self.session.client("route53") as client:  # type: ignore
            try:
                await client.change_resource_record_sets(
                    HostedZoneId=self._zone_id,
                    ChangeBatch={
                        "Changes": [
                            {
                                "Action": "DELETE",
                                "ResourceRecordSet": {
                                    "Name": self.get_cname_domain(name),
                                    "Type": "CNAME",
                                    "TTL": 300,
                                    "ResourceRecords": [{"Value": current_record}],
                                },
                            }
                        ]
                    },
                )
            except client.exceptions.InvalidChangeBatch:
                # Record doesn't exist, which is fine since we want to delete it anyway
                return

    async def update_cname_record(self, name: str, new_value: str) -> None:
        """Update a CNAME record for a machine.

        Args:
            name: name to update the record for
            new_value: new value for the CNAME record
        """
        print(f"Updating CNAME record for {name} to {new_value}")
        async with self.session.client("route53") as client:  # type: ignore
            await client.change_resource_record_sets(
                HostedZoneId=self._zone_id,
                ChangeBatch={
                    "Changes": [
                        {
                            "Action": "UPSERT",
                            "ResourceRecordSet": {
                                "Name": self.get_cname_domain(name),
                                "Type": "CNAME",
                                "TTL": 300,
                                "ResourceRecords": [{"Value": new_value}],
                            },
                        }
                    ]
                },
            )

    async def get_cname_record(self, name: str) -> str | None:
        # Format the DNS name as a subdomain of the hosted zone
        dns_name = self.get_cname_domain(name)

        async with self.session.client("route53") as client:  # type: ignore
            response = await client.list_resource_record_sets(
                HostedZoneId=self._zone_id,
                StartRecordName=dns_name,
                StartRecordType="CNAME",
            )

            if (
                not response.get("ResourceRecordSets")
                or len(response["ResourceRecordSets"]) == 0
            ):
                return None

            return response["ResourceRecordSets"][0]["ResourceRecords"][0]["Value"]
