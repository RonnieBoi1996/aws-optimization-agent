"""On-demand prices from the AWS Pricing API (pricing:GetProducts), cached for a week.

Used to put a dollar figure on idle instances and unattached volumes. If a
lookup fails we return None and the tool says the cost is unknown; we never
guess a price.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from optimizer.insights.cache import cached

log = logging.getLogger(__name__)
WEEK = 7 * 24 * 3600
HOURS_PER_MONTH = 730  # AWS's own convention for monthly estimates

# The Pricing API only has a few endpoints; us-east-1 is one of them.
PRICING_REGION = "us-east-1"


def _first_usd_price(price_list: list[str]) -> float | None:
    """Pull the on-demand USD price out of GetProducts' nested JSON strings."""
    for item in price_list:
        product = json.loads(item)
        for term in product.get("terms", {}).get("OnDemand", {}).values():
            for dim in term.get("priceDimensions", {}).values():
                usd = dim.get("pricePerUnit", {}).get("USD")
                if usd is not None and float(usd) > 0:
                    return float(usd)
    return None


def _lookup(pricing_client: Any, service_code: str, filters: dict[str, str]) -> float | None:
    def fetch():
        resp = pricing_client.get_products(
            ServiceCode=service_code,
            Filters=[{"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in sorted(filters.items())],
            MaxResults=10,
        )
        return _first_usd_price(resp.get("PriceList", []))

    try:
        value, _ = cached("pricing", {"service": service_code, **filters}, WEEK, fetch)
        return value
    except Exception as e:  # pricing is a nice-to-have; never fail a tool over it
        log.warning("price lookup failed for %s %s: %s", service_code, filters, e)
        return None


def ec2_hourly_usd(pricing_client: Any, region: str, instance_type: str) -> float | None:
    """Linux, shared tenancy, on-demand."""
    return _lookup(pricing_client, "AmazonEC2", {
        "regionCode": region,
        "instanceType": instance_type,
        "operatingSystem": "Linux",
        "tenancy": "Shared",
        "preInstalledSw": "NA",
        "capacitystatus": "Used",
    })


def ebs_gb_month_usd(pricing_client: Any, region: str, volume_type: str) -> float | None:
    """Storage price per GB-month (not IOPS/throughput extras)."""
    return _lookup(pricing_client, "AmazonEC2", {
        "regionCode": region,
        "productFamily": "Storage",
        "volumeApiName": volume_type,
    })
