import aioboto3

from machines.config import app_config


class Route53Service:
    def __init__(self):
        self.session = aioboto3.Session(
            aws_access_key_id=app_config.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=app_config.AWS_SECRET_ACCESS_KEY,
            region_name=app_config.AWS_REGION,
        )
        self.domain = "lazycloud.dev"

    def get_cname_domain(self, name: str) -> str:
        return f"{name}.{self.domain}."

    async def create_cname_record(self, name: str) -> None:
        """Create a CNAME record for a machine.

        Args:
            name: name to create the record for
        """
        print(f"Creating CNAME record for {name}")
        fly_app_name = f"{name}.fly.dev"
        async with self.session.client("route53") as client:
            await client.change_resource_record_sets(
                HostedZoneId=app_config.AWS_ROUTE53_ZONE_ID,
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

        async with self.session.client("route53") as client:
            await client.change_resource_record_sets(
                HostedZoneId=app_config.AWS_ROUTE53_ZONE_ID,
                ChangeBatch={
                    "Changes": [
                        {
                            "Action": "DELETE",
                            "ResourceRecordSet": {
                                "Name": self.get_cname_domain(name),
                                "Type": "CNAME",
                                "TTL": 300,
                                "ResourceRecords": [{"Value": current_record}],
                            }
                        }
                    ]
                },
            )

    async def update_cname_record(self, name: str, new_value: str) -> None:
        """Update a CNAME record for a machine.

        Args:
            name: name to update the record for
            new_value: new value for the CNAME record
        """
        print(f"Updating CNAME record for {name} to {new_value}")
        async with self.session.client("route53") as client:
            await client.change_resource_record_sets(
                HostedZoneId=app_config.AWS_ROUTE53_ZONE_ID,
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


    async def get_cname_record(self, name: str) -> str:
        # Format the DNS name as a subdomain of the hosted zone
        dns_name = self.get_cname_domain(name)

        async with self.session.client("route53") as client:
            response = await client.list_resource_record_sets(
                HostedZoneId=app_config.AWS_ROUTE53_ZONE_ID,
                StartRecordName=dns_name,
                StartRecordType="CNAME",
            )

            if not response.get("ResourceRecordSets") or len(response["ResourceRecordSets"]) == 0:
                raise Exception(f"CNAME record for {name} not found")

            return response["ResourceRecordSets"][0]["ResourceRecords"][0]["Value"]
