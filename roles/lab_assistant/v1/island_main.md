Improve the Newton-Raphson reference circuit for the invsqrt phase oracle.
Lower the cost Q x (Toffolis + T/4): fewer qubits and fewer Toffolis both count.
Keep the x register restored and every scratch qubit back at |0>, or the circuit fails verification.
