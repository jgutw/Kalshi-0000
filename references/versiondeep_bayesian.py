import numpy as np
from scipy.stats import beta
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from utils import logger, ColoredFormatter as cf

class BayesianProbabilityUpdater:
    """
    Implements Bayesian probability updates for prediction markets
    
    Core formula: P(H|E) = P(E|H) * P(H) / P(E)
    
    Supports:
    - Prior updates from multiple signals
    - Time decay of information
    - Signal weighting by reliability
    """
    
    def __init__(self, prior: float = 0.5, confidence: float = 1.0):
        """
        Initialize with Beta distribution prior
        
        Args:
            prior: Initial probability (0-1)
            confidence: Prior confidence (higher = more weight)
        """
        # Convert to Beta parameters
        # alpha = prior * confidence
        # beta = (1-prior) * confidence
        self.alpha = prior * confidence
        self.beta = (1 - prior) * confidence
        
        self.prior = prior
        self.signals = []
        
    def update(self, 
               likelihood: float, 
               signal_weight: float = 1.0,
               signal_name: str = "") -> float:
        """
        Update probability using Bayes' theorem
        
        Args:
            likelihood: P(E|H) - probability of seeing this signal if hypothesis is true
            signal_weight: Reliability/weight of this signal
            signal_name: Optional name for logging
        
        Returns:
            Updated posterior probability
        """
        # Store signal
        self.signals.append({
            'timestamp': datetime.now(),
            'likelihood': likelihood,
            'weight': signal_weight,
            'name': signal_name
        })
        
        # Apply time decay to existing signals
        self._apply_time_decay()
        
        # Calculate posterior using Beta update
        # For a Beta prior, each signal updates alpha and beta
        if likelihood > 0.5:
            # Positive evidence
            self.alpha += signal_weight * (likelihood - 0.5) * 2
        else:
            # Negative evidence
            self.beta += signal_weight * (0.5 - likelihood) * 2
        
        # Calculate posterior mean
        posterior = self.alpha / (self.alpha + self.beta)
        
        logger.debug(cf.info(
            f"Bayes update: {signal_name} "
            f"(likelihood={likelihood:.3f}, weight={signal_weight:.2f}) "
            f"prior={self.prior:.3f} → posterior={posterior:.3f}"
        ))
        
        self.prior = posterior
        return posterior
    
    def _apply_time_decay(self, half_life_hours: float = 1.0):
        """
        Apply exponential time decay to signal weights
        
        Formula: w_new = w_old * exp(-λ * Δt)
        where λ = ln(2) / half_life
        """
        now = datetime.now()
        lambda_decay = np.log(2) / (half_life_hours * 3600)  # per second
        
        for signal in self.signals:
            age = (now - signal['timestamp']).total_seconds()
            decay = np.exp(-lambda_decay * age)
            signal['weight'] *= decay
        
        # Remove signals with negligible weight
        self.signals = [s for s in self.signals if s['weight'] > 0.01]
    
    def get_confidence(self) -> float:
        """
        Get confidence in current estimate
        
        Higher confidence when we have more total evidence
        """
        total_evidence = (self.alpha + self.beta) - 2  # Subtract prior
        # Normalize to 0-1 (assuming max evidence of 100)
        return min(1.0, total_evidence / 100)
    
    def get_predictive_distribution(self, n_samples: int = 1000) -> np.ndarray:
        """Sample from posterior predictive distribution"""
        return np.random.beta(self.alpha, self.beta, n_samples)
    
    def get_credible_interval(self, interval: float = 0.95) -> Tuple[float, float]:
        """Get credible interval around current estimate"""
        lower = (1 - interval) / 2
        upper = 1 - lower
        return (beta.ppf(lower, self.alpha, self.beta),
                beta.ppf(upper, self.alpha, self.beta))

class MultiSourceBayesianFusion:
    """
    Fuse multiple Bayesian updaters with different signal sources
    
    Uses inverse-variance weighting for optimal fusion
    """
    
    def __init__(self):
        self.sources: Dict[str, BayesianProbabilityUpdater] = {}
        self.weights: Dict[str, float] = {}
        
    def add_source(self, 
                   name: str, 
                   prior: float = 0.5, 
                   confidence: float = 1.0,
                   weight: float = 1.0):
        """Add a new signal source"""
        self.sources[name] = BayesianProbabilityUpdater(prior, confidence)
        self.weights[name] = weight
    
    def update_source(self, 
                      name: str, 
                      likelihood: float, 
                      signal_weight: float = 1.0) -> float:
        """Update a specific source"""
        if name not in self.sources:
            raise ValueError(f"Unknown source: {name}")
        
        return self.sources[name].update(likelihood, signal_weight, name)
    
    def fuse(self) -> Tuple[float, float]:
        """
        Fuse all sources using inverse-variance weighting
        
        Returns:
            (fused_probability, uncertainty)
        """
        if not self.sources:
            return 0.5, 1.0
        
        weighted_sum = 0.0
        total_weight = 0.0
        
        for name, source in self.sources.items():
            prob = source.prior
            # Uncertainty is variance of Beta distribution
            variance = (source.alpha * source.beta) / (
                (source.alpha + source.beta)**2 * 
                (source.alpha + source.beta + 1)
            )
            
            # Weight = 1/variance (inverse-variance weighting)
            weight = self.weights.get(name, 1.0) / (variance + 1e-8)
            
            weighted_sum += prob * weight
            total_weight += weight
        
        fused_prob = weighted_sum / total_weight if total_weight > 0 else 0.5
        
        # Combined uncertainty
        fused_uncertainty = 1.0 / np.sqrt(total_weight) if total_weight > 0 else 1.0
        
        return fused_prob, fused_uncertainty
    
    def get_status(self) -> str:
        """Get formatted status of all sources"""
        lines = ["\nBayesian Fusion Status:"]
        for name, source in self.sources.items():
            ci_lower, ci_upper = source.get_credible_interval()
            lines.append(
                f"  {name}: {source.prior:.3f} "
                f"[{ci_lower:.3f}-{ci_upper:.3f}] "
                f"(conf={source.get_confidence():.2f})"
            )
        
        fused, unc = self.fuse()
        lines.append(f"  FUSED: {fused:.3f} ± {unc:.3f}")
        
        return "\n".join(lines)


