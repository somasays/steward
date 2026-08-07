"""What the allowlist admits, and everything it refuses (I15).

The property under test is asymmetric on purpose: admitting one endpoint too many is
a data leak, refusing one too many is an outage that is loud. So every ambiguous case
here — an unparseable URL, an empty allowlist, a sibling path — is asserted to refuse.
"""

import pytest
from steward_llm.endpoints import (
    Endpoint,
    EndpointAllowlist,
    MalformedEndpoint,
    NonApprovedEndpoint,
)

VLLM = "http://vllm-a.steward-inference.svc.cluster.local:8000/v1"


class TestNormalisation:
    def test_the_default_port_is_made_explicit(self) -> None:
        assert Endpoint.parse("http://vllm.internal/v1").port == 80
        assert Endpoint.parse("https://vllm.internal/v1").port == 443

    def test_an_explicit_default_port_is_the_same_endpoint(self) -> None:
        assert Endpoint.parse("http://vllm.internal:80/v1") == Endpoint.parse("http://vllm.internal/v1")

    def test_scheme_and_host_are_case_insensitive(self) -> None:
        assert Endpoint.parse("HTTP://VLLM.Internal:8000/v1") == Endpoint.parse(
            "http://vllm.internal:8000/v1"
        )

    def test_a_trailing_slash_is_not_a_different_endpoint(self) -> None:
        assert Endpoint.parse("http://vllm.internal:8000/v1/") == Endpoint.parse(
            "http://vllm.internal:8000/v1"
        )

    def test_surrounding_whitespace_is_ignored(self) -> None:
        assert Endpoint.parse(f"  {VLLM}  ") == Endpoint.parse(VLLM)

    def test_it_prints_as_a_url(self) -> None:
        assert str(Endpoint.parse(VLLM)) == VLLM


class TestRefusedUrls:
    """A URL this module cannot normalise is refused, never compared optimistically."""

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "   ",
            "vllm.internal:8000",
            "ftp://vllm.internal/v1",
            "file:///etc/passwd",
            "http://user:token@vllm.internal:8000/v1",
            "http://vllm.internal:8000/v1?key=secret",
            "http://vllm.internal:8000/v1#frag",
            "http://vllm.internal:not-a-port/v1",
            "http:///v1",
        ],
    )
    def test_it_raises(self, url: str) -> None:
        with pytest.raises(MalformedEndpoint):
            Endpoint.parse(url)


class TestCoverage:
    def test_a_path_below_an_approved_endpoint_is_covered(self) -> None:
        allowlist = EndpointAllowlist.from_urls([VLLM])
        assert allowlist.admits(f"{VLLM}/chat/completions")

    def test_an_origin_without_a_path_covers_every_path_on_it(self) -> None:
        allowlist = EndpointAllowlist.from_urls(["http://vllm.internal:8000"])
        assert allowlist.admits("http://vllm.internal:8000/v1")

    def test_a_sibling_path_is_not_covered(self) -> None:
        allowlist = EndpointAllowlist.from_urls([VLLM])
        assert not allowlist.admits(f"{VLLM}beta")

    @pytest.mark.parametrize(
        "url",
        [
            "http://vllm-b.steward-inference.svc.cluster.local:8000/v1",
            "http://vllm-a.steward-inference.svc.cluster.local:9000/v1",
            "https://vllm-a.steward-inference.svc.cluster.local:8000/v1",
        ],
    )
    def test_another_origin_is_not_covered(self, url: str) -> None:
        assert not EndpointAllowlist.from_urls([VLLM]).admits(url)

    def test_an_empty_allowlist_admits_nothing(self) -> None:
        assert not EndpointAllowlist.from_urls([]).admits(VLLM)

    def test_a_malformed_candidate_raises_rather_than_returning_false(self) -> None:
        with pytest.raises(MalformedEndpoint):
            EndpointAllowlist.from_urls([VLLM]).admits("not-a-url")


class TestHostedProviderBackstop:
    @pytest.mark.parametrize(
        "url",
        [
            "https://api.openai.com/v1",
            "https://steward.openai.azure.com/openai/deployments/x",
            "https://api.anthropic.com",
            "https://generativelanguage.googleapis.com/v1beta",
            "https://bedrock-runtime.us-east-1.amazonaws.com",
            "https://api.mistral.ai/v1",
            "https://openrouter.ai/api/v1",
        ],
    )
    def test_a_hosted_api_cannot_be_approved(self, url: str) -> None:
        with pytest.raises(NonApprovedEndpoint):
            EndpointAllowlist.from_urls([VLLM, url])

    def test_a_hosted_api_is_refused_even_beneath_an_approved_origin(self) -> None:
        """The deny layer is checked on the candidate, not only on the list."""
        allowlist = EndpointAllowlist.from_urls([VLLM])
        assert not allowlist.admits("https://api.openai.com/v1")

    def test_a_self_hosted_host_on_a_cloud_domain_is_still_allowed(self) -> None:
        """The deny layer names hosted inference APIs, not cloud providers — an internal
        load balancer on amazonaws.com is a legitimate self-hosted endpoint."""
        internal = "http://internal-vllm-1234.eu-west-1.elb.amazonaws.com:8000/v1"
        assert EndpointAllowlist.from_urls([internal]).admits(internal)
