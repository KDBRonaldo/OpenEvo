from openhands.agenthub.gui_agent.gui_agent import (
    GuiAgent,
)

from openhands.controller.agent import Agent
Agent.register('GuiAgent', GuiAgent)
