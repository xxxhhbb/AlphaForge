from veriquant.research_os_v3 import terminal


def test_legacy_terminal_module_forwards_to_agent_research_product():
    assert terminal.main.__module__ == "veriquant.agent_research_product.terminal"
    assert terminal.run.__module__ == "veriquant.agent_research_product.terminal"
