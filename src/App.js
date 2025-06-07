import React, { useState } from "react";
import axios from "axios";

function App() {
  const [ticker, setTicker] = useState("");
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const fetchDCF = async () => {
    setLoading(true);
    setError("");
    try {
      const response = await axios.get(`http://127.0.0.1:8000/dcf?ticker=${ticker}`);
      setResult(response.data);
    } catch (err) {
      setError("Failed to fetch data. Please check the ticker or backend.");
      console.error(err);
    }
    setLoading(false);
  };

  return (
    <div className="min-h-screen flex flex-col items-center justify-center bg-gray-100 p-6">
      <h1 className="text-3xl font-bold mb-4">Mini Capital IQ - DCF Calculator</h1>
      <input
        type="text"
        value={ticker}
        onChange={(e) => setTicker(e.target.value)}
        placeholder="Enter stock ticker (e.g., AAPL)"
        className="border p-2 rounded mb-4 w-64"
      />
      <button
        onClick={fetchDCF}
        disabled={loading || !ticker}
        className="bg-blue-600 text-white px-4 py-2 rounded hover:bg-blue-700 disabled:bg-gray-400"
      >
        {loading ? "Loading..." : "Get DCF"}
      </button>

      {error && <p className="text-red-600 mt-4">{error}</p>}

      {result && !result.error && (
        <div className="bg-white p-4 rounded shadow mt-6 w-96 text-left">
          <h2 className="text-xl font-semibold mb-2">{result.ticker}</h2>
          <p><strong>Intrinsic Value:</strong> ${result.intrinsicValue.toFixed(2)}</p>
          <p><strong>Current Price:</strong> ${result.currentPrice}</p>
          <p><strong>WACC:</strong> {result.wacc}%</p>
          <p><strong>Terminal Growth:</strong> {result.terminalGrowth}%</p>
        </div>
      )}

      {result?.error && (
        <p className="text-red-500 mt-4">Error: {result.error}</p>
      )}
    </div>
  );
}

export default App;
