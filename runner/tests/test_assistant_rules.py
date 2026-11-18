"""Who pays for the assistant's Claude. One developer, one factory, one assistant: it runs on that developer's own
subscription, as it always did, and may be switched to an API key. Its Slack, the people it names and its Night
Shift ticket desk are the same either way: nothing is refused for them."""

import pytest
import yaml

from arceus_runner.config import ConfigError, parse_config

from .conftest import EXAMPLE

DESK = {"bridge": "sudo -n /usr/local/bin/fa-ticket-engine"}
SLACK = {"read_channels": ["C0AAAAAAA1"], "creator_channels": ["C0AAAAAAA1"], "creator_domain": "example.com"}


def config(**assistant) -> str:
    raw = yaml.safe_load(EXAMPLE.read_text())
    raw["assistant"] = {"enabled": True, "allowed_users": ["dev@example.com"], **assistant}
    return yaml.safe_dump(raw, allow_unicode=True)


def test_the_developers_own_subscription_is_the_default_with_slack_people_and_the_desk():
    a = parse_config(config(slack=SLACK, ticket_creators=["dev@example.com", "pm@example.com"],
                            ticket_engine=DESK)).assistant
    assert a.auth == "subscription" and a.ticket_engine and a.slack


@pytest.mark.parametrize("auth", ["subscription", "api_key"])
def test_either_way_the_desk_and_slack_stay(auth):
    a = parse_config(config(auth=auth, slack=SLACK, ticket_engine=DESK)).assistant
    assert a.auth == auth and a.ticket_engine.bridge.endswith("fa-ticket-engine")


def test_an_unknown_way_to_pay_is_refused():
    with pytest.raises(ConfigError, match="auth"):
        parse_config(config(auth="deepseek"))
