# -*- coding: utf-8 -*-
from qwenpaw_data.host.core.runtime.builder.data_agent import DataAgentBuilder
from qwenpaw_data.host.core.runtime.builder.review_agent import ReviewAgentBuilder

__all__ = [
    "DataAgentBuilder",
    "ReviewAgentBuilder",
    "get_agent_builder",
]


def get_agent_builder(agent_id: str) -> DataAgentBuilder | ReviewAgentBuilder:
    if ReviewAgentBuilder.matches(agent_id):
        return ReviewAgentBuilder()
    return DataAgentBuilder()
