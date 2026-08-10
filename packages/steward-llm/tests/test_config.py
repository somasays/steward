"""The startup refusal: which gateway configurations a process will boot with (I15).

The committed production config is asserted to pass the same check that a config
reaching a hosted API fails, so "approved is accepted" and "non-approved is refused"
are proved against the file this repo actually ships.
"""

from decimal import Decimal
from pathlib import Path

import pytest
from steward_llm import config as gateway
from steward_llm.config import (
    APPROVED_ENDPOINTS_ENV,
    CONFIG_PATH_ENV,
    MODE_ENV,
    PRODUCTION_ALIASES,
    DeploymentMode,
    GatewayConfig,
    InvalidGatewayConfig,
    ModelBinding,
    TokenPricing,
    committed_production_config,
    gateway_config_from_env,
    load_approved_endpoints,
    parse_litellm_config,
)
from steward_llm.endpoints import EndpointAllowlist, NonApprovedEndpoint
from steward_llm.validate import main as validate_main

PRICES = {
    "input_cost_per_token": "0.0000001",
    "output_cost_per_token": "0.0000003",
    "chat_template_tokens_per_message": 8,
}
PRICING = TokenPricing(
    input_cost_per_token=Decimal("0.0000001"),
    output_cost_per_token=Decimal("0.0000003"),
    chat_template_tokens_per_message=8,
)

APPROVED = "http://vllm-reasoning-a.steward-inference.svc.cluster.local:8000/v1"
ALLOWLIST = EndpointAllowlist.from_urls([APPROVED])


def bindings(**overrides: ModelBinding) -> tuple[ModelBinding, ...]:
    """A complete production routing table, with named aliases replaced."""
    table = {
        alias: ModelBinding(
            alias=alias,
            model="hosted_vllm/qwen3-32b-instruct",
            api_base=APPROVED,
            pricing=PRICING,
        )
        for alias in sorted(PRODUCTION_ALIASES)
    }
    table.update({binding.alias: binding for binding in overrides.values()})
    return tuple(table.values())


def config(**overrides: ModelBinding) -> GatewayConfig:
    return GatewayConfig(
        mode=DeploymentMode.PRODUCTION, source="test", bindings=bindings(**overrides), allowlist=ALLOWLIST
    )


def write(tmp_path: Path, body: str, name: str = "litellm.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


class TestTheCommittedConfig:
    def test_it_boots(self) -> None:
        """An approved endpoint is accepted: the shipped config passes the startup check."""
        validated = committed_production_config()
        assert validated.mode is DeploymentMode.PRODUCTION
        assert {binding.alias for binding in validated.bindings} == set(PRODUCTION_ALIASES)

    def test_every_alias_has_a_second_endpoint(self) -> None:
        """Hosted fallbacks are gone, so redundancy has to live inside the allowlist."""
        for alias in PRODUCTION_ALIASES:
            endpoints = {b.api_base for b in committed_production_config().bindings if b.alias == alias}
            assert len(endpoints) >= 2, alias

    def test_the_validator_entry_point_reports_it(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert validate_main() == 0
        assert "gateway config OK" in capsys.readouterr().out


class TestProductionRefusals:
    def test_a_hosted_api_base_refuses(self) -> None:
        """The mistake this invariant exists for: a base URL reaching a hosted API."""
        with pytest.raises(NonApprovedEndpoint, match="not an approved"):
            config(
                reasoning=ModelBinding(
                    alias="steward-reasoning",
                    model="hosted_vllm/qwen3-32b-instruct",
                    api_base="https://api.openai.com/v1",
                    pricing=PRICING,
                )
            )

    def test_an_unapproved_internal_endpoint_refuses(self) -> None:
        with pytest.raises(NonApprovedEndpoint, match="not an approved"):
            config(
                fast=ModelBinding(
                    alias="steward-fast",
                    model="hosted_vllm/qwen3-8b-instruct",
                    api_base="http://vllm-rogue.other.svc.cluster.local:8000/v1",
                    pricing=PRICING,
                )
            )

    def test_a_missing_api_base_refuses(self) -> None:
        """No URL at all is the quietest breach: the provider's own API is the default."""
        with pytest.raises(NonApprovedEndpoint, match="no api_base"):
            config(
                fast=ModelBinding(
                    alias="steward-fast",
                    model="claude-sonnet-5",
                    api_base=None,
                    pricing=PRICING,
                )
            )

    def test_a_provider_routed_model_refuses_even_on_an_approved_url(self) -> None:
        with pytest.raises(NonApprovedEndpoint, match="chooses the destination"):
            config(
                fast=ModelBinding(
                    alias="steward-fast",
                    model="anthropic/claude-sonnet-5",
                    api_base=APPROVED,
                    pricing=PRICING,
                )
            )

    def test_an_empty_allowlist_refuses(self) -> None:
        with pytest.raises(NonApprovedEndpoint, match="no approved endpoints"):
            GatewayConfig(
                mode=DeploymentMode.PRODUCTION,
                source="test",
                bindings=bindings(),
                allowlist=EndpointAllowlist.from_urls([]),
            )

    def test_a_missing_alias_refuses(self) -> None:
        table = tuple(b for b in bindings() if b.alias != "steward-embed")
        with pytest.raises(InvalidGatewayConfig, match="steward-embed"):
            GatewayConfig(mode=DeploymentMode.PRODUCTION, source="test", bindings=table, allowlist=ALLOWLIST)

    def test_an_unused_hosted_entry_refuses_too(self) -> None:
        """Every model_list entry is validated, not only the named aliases — LiteLLM can
        route to anything in the file through a fallback chain."""
        table = bindings() + (
            ModelBinding(
                alias="scratch",
                model="openai/gpt-5",
                api_base="https://api.openai.com/v1",
                pricing=PRICING,
            ),
        )
        with pytest.raises(NonApprovedEndpoint):
            GatewayConfig(mode=DeploymentMode.PRODUCTION, source="test", bindings=table, allowlist=ALLOWLIST)


class TestDevelopmentMode:
    def test_it_accepts_a_hosted_binding(self) -> None:
        validated = GatewayConfig(
            mode=DeploymentMode.DEVELOPMENT,
            source="test",
            allowlist=EndpointAllowlist.from_urls([]),
            bindings=(ModelBinding(
                alias="steward-fast",
                model="claude-haiku-4-5",
                api_base=None,
                pricing=PRICING,
            ),),
        )
        assert validated.mode is DeploymentMode.DEVELOPMENT

    def test_production_is_the_default_mode(self) -> None:
        assert gateway.mode_from_env({}) is DeploymentMode.PRODUCTION
        assert gateway.mode_from_env({MODE_ENV: "  "}) is DeploymentMode.PRODUCTION

    def test_it_is_selected_by_name_only(self) -> None:
        assert gateway.mode_from_env({MODE_ENV: "DEVELOPMENT"}) is DeploymentMode.DEVELOPMENT
        with pytest.raises(InvalidGatewayConfig, match="not a deployment mode"):
            gateway.mode_from_env({MODE_ENV: "dev"})


class TestPassThroughRoutes:
    """`pass_through_endpoints` maps a proxy path onto a target URL without touching
    `model_list` — routing under another name, so it faces the same allowlist."""

    def route(self, target: str) -> dict[str, object]:
        return {"general_settings": {"pass_through_endpoints": [{"path": "/vertex", "target": target}]}}

    def parsed(self, target: str) -> tuple[ModelBinding, ...]:
        document: dict[str, object] = {
            "model_list": [
                {
                    "model_name": alias,
                    "litellm_params": {"model": "hosted_vllm/qwen3-8b", "api_base": APPROVED},
                    "model_info": PRICES,
                }
                for alias in sorted(PRODUCTION_ALIASES)
            ]
        }
        document.update(self.route(target))
        return parse_litellm_config(document, "test")

    def test_a_route_to_a_hosted_api_refuses(self) -> None:
        with pytest.raises(NonApprovedEndpoint, match="pass_through /vertex"):
            GatewayConfig(
                mode=DeploymentMode.PRODUCTION,
                source="test",
                allowlist=ALLOWLIST,
                bindings=self.parsed("https://generativelanguage.googleapis.com/v1beta"),
            )

    def test_a_route_to_an_approved_endpoint_is_accepted(self) -> None:
        assert (
            GatewayConfig(
                mode=DeploymentMode.PRODUCTION,
                source="test",
                allowlist=ALLOWLIST,
                bindings=self.parsed(APPROVED),
            )
            .bindings[-1]
            .alias
            == "pass_through /vertex"
        )

    @pytest.mark.parametrize(
        "settings",
        [
            "pass_through_endpoints",
            {"pass_through_endpoints": {"path": "/vertex"}},
            {"pass_through_endpoints": ["/vertex"]},
            {"pass_through_endpoints": [{"target": "http://vllm:8000"}]},
            {"pass_through_endpoints": [{"path": "/vertex"}]},
            {"pass_through_endpoints": [{"path": "/vertex", "target": 8000}]},
        ],
    )
    def test_an_unreadable_route_refuses(self, settings: object) -> None:
        with pytest.raises(InvalidGatewayConfig):
            parse_litellm_config(
                {
                    "model_list": [
                        {
                            "model_name": "steward-fast",
                            "litellm_params": {"model": "hosted_vllm/x", "api_base": APPROVED},
                            "model_info": PRICES,
                        }
                    ],
                    "general_settings": settings,
                },
                "test",
            )

    def test_a_config_without_routes_gains_no_bindings(self) -> None:
        base = {
            "model_list": [
                {
                    "model_name": "steward-fast",
                    "litellm_params": {"model": "hosted_vllm/x", "api_base": APPROVED},
                    "model_info": PRICES,
                }
            ]
        }
        assert len(parse_litellm_config(base, "test")) == 1
        assert len(parse_litellm_config({**base, "general_settings": {"master_key": "x"}}, "test")) == 1


class TestParsing:
    def test_it_reads_alias_model_and_base(self) -> None:
        document = {
            "model_list": [
                {
                    "model_name": "steward-fast",
                    "litellm_params": {"model": "hosted_vllm/qwen3-8b", "api_base": APPROVED},
                    "model_info": PRICES,
                }
            ]
        }
        assert parse_litellm_config(document, "test") == (
            ModelBinding(
                alias="steward-fast",
                model="hosted_vllm/qwen3-8b",
                api_base=APPROVED,
                pricing=PRICING,
            ),
        )

    @pytest.mark.parametrize(
        "document",
        [
            None,
            ["model_list"],
            {"model_list": []},
            {"model_list": {"steward-fast": APPROVED}},
            {"model_list": ["steward-fast"]},
            {"model_list": [{"litellm_params": {"model": "hosted_vllm/x"}}]},
            {"model_list": [{"model_name": "", "litellm_params": {"model": "hosted_vllm/x"}}]},
            {"model_list": [{"model_name": "steward-fast"}]},
            {"model_list": [{"model_name": "steward-fast", "litellm_params": []}]},
            {"model_list": [{"model_name": "steward-fast", "litellm_params": {}}]},
            {
                "model_list": [
                    {"model_name": "steward-fast", "litellm_params": {"model": "x", "api_base": 8000}}
                ]
            },
        ],
    )
    def test_an_unreadable_shape_refuses(self, document: object) -> None:
        with pytest.raises(InvalidGatewayConfig):
            parse_litellm_config(document, "test")

    def test_invalid_yaml_refuses(self, tmp_path: Path) -> None:
        with pytest.raises(InvalidGatewayConfig, match="not valid YAML"):
            gateway.load_litellm_config(write(tmp_path, "model_list: [{a: :}]"))

    def test_a_missing_file_refuses(self, tmp_path: Path) -> None:
        with pytest.raises(InvalidGatewayConfig, match="cannot be read"):
            gateway.load_litellm_config(tmp_path / "absent.yaml")

    def test_an_allowlist_file_must_hold_a_list_of_urls(self, tmp_path: Path) -> None:
        with pytest.raises(InvalidGatewayConfig, match="approved_endpoints"):
            load_approved_endpoints(write(tmp_path, "approved_endpoints: nope\n"))
        with pytest.raises(InvalidGatewayConfig, match="cannot be read"):
            load_approved_endpoints(tmp_path / "absent.yaml")
        with pytest.raises(InvalidGatewayConfig, match="not valid YAML"):
            load_approved_endpoints(write(tmp_path, "approved_endpoints: [:"))


def yaml_config(api_base: str | None) -> str:
    """A complete routing table as a LiteLLM config file. `None` omits every api_base,
    which is how a config reaches a hosted provider without naming a URL."""
    lines = ["model_list:"]
    for alias in sorted(PRODUCTION_ALIASES):
        lines += [f"  - model_name: {alias}", "    litellm_params:"]
        lines.append("      model: hosted_vllm/qwen3-8b" if api_base else "      model: claude-sonnet-5")
        if api_base is not None:
            lines.append(f"      api_base: {api_base}")
        lines += [
            "    model_info:",
            "      input_cost_per_token: 0.0000001",
            "      output_cost_per_token: 0.0000003",
            "      chat_template_tokens_per_message: 8",
        ]
    return "\n".join(lines) + "\n"


PROD_YAML = yaml_config(APPROVED)
HOSTED_YAML = yaml_config(None)


class TestStartupFromTheEnvironment:
    def test_no_config_path_means_no_gateway(self) -> None:
        assert gateway_config_from_env({}) is None
        assert gateway_config_from_env({CONFIG_PATH_ENV: "  "}) is None

    def test_a_config_on_approved_endpoints_boots(self, tmp_path: Path) -> None:
        validated = gateway_config_from_env(
            {CONFIG_PATH_ENV: str(write(tmp_path, PROD_YAML)), APPROVED_ENDPOINTS_ENV: f" {APPROVED} , "}
        )
        assert validated is not None
        assert len(validated.bindings) == len(PRODUCTION_ALIASES)

    def test_a_config_reaching_a_hosted_api_refuses_to_boot(self, tmp_path: Path) -> None:
        with pytest.raises(NonApprovedEndpoint):
            gateway_config_from_env(
                {CONFIG_PATH_ENV: str(write(tmp_path, HOSTED_YAML)), APPROVED_ENDPOINTS_ENV: APPROVED}
            )

    def test_it_falls_back_to_the_committed_allowlist(self, tmp_path: Path) -> None:
        """No allowlist in the environment is not an empty allowlist — it is this repo's."""
        committed = load_approved_endpoints(gateway.COMMITTED_ALLOWLIST)
        body = PROD_YAML.replace(APPROVED, committed[0])
        assert gateway_config_from_env({CONFIG_PATH_ENV: str(write(tmp_path, body))}) is not None

    def test_the_process_environment_is_the_default_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(CONFIG_PATH_ENV, raising=False)
        assert gateway_config_from_env() is None

    def test_the_validator_entry_point_reports_a_refusal(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(gateway, "COMMITTED_CONFIG", write(tmp_path, HOSTED_YAML))
        assert validate_main() == 1
        assert "REFUSED" in capsys.readouterr().out
