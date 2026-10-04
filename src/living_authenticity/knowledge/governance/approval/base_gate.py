"""Approval gate interface."""
from abc import ABC, abstractmethod


class ApprovalGate(ABC):
    """Interface for explicit single-proposal human approval."""

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def build_request(self, query, proposal=None, confidence=None, note=None):
        raise NotImplementedError

    @abstractmethod
    def display(self, request) -> str:
        raise NotImplementedError

    @abstractmethod
    def decide(self, request, raw_input, timestamp: str = ""):
        raise NotImplementedError

    @abstractmethod
    def request_approval(self, query, proposal=None, confidence=None,
                         reader=None, note=None):
        raise NotImplementedError
