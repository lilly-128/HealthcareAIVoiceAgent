import { useState } from "react";
import { api } from "../api";

export default function Login({ onLogin }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleLogin = async (e) => {
    e.preventDefault();

    setError("");
    setLoading(true);

    try {
      // Login request
      const data = await api.login(email, password);

      console.log("LOGIN RESPONSE:", data);

      // Make sure token is saved
      if (!data.access_token) {
        throw new Error("Login successful but no access token was received.");
      }

      localStorage.setItem("token", data.access_token);
      localStorage.setItem("role", data.role || "");

      // Save hospital ID for Hospital Admin
      if (data.hospital_id) {
        localStorage.setItem("hospital_id", data.hospital_id);
      } else {
        localStorage.removeItem("hospital_id");
      }

      // Save patient ID
      if (data.patient_id) {
        localStorage.setItem("patient_id", data.patient_id);
      } else {
        localStorage.removeItem("patient_id");
      }

      // Save doctor ID
      if (data.doctor_id) {
        localStorage.setItem("doctor_id", data.doctor_id);
      } else {
        localStorage.removeItem("doctor_id");
      }

      // Verify that token is actually stored
      console.log(
        "TOKEN SAVED:",
        localStorage.getItem("token")
          ? "YES"
          : "NO"
      );

      console.log(
        "ROLE:",
        localStorage.getItem("role")
      );

      console.log(
        "HOSPITAL ID:",
        localStorage.getItem("hospital_id")
      );

      // Only move to the next page after localStorage is updated
      onLogin(data);

    } catch (err) {
      console.error("LOGIN ERROR:", err);
      setError(err.message || "Login failed.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login-page">
      <div className="login-card">
        <h1>Healthcare AI</h1>
        <h2>Login</h2>

        <form onSubmit={handleLogin}>
          <label>Email</label>

          <input
            type="email"
            placeholder="Enter your email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />

          <label>Password</label>

          <input
            type="password"
            placeholder="Enter your password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />

          {error && (
            <p className="error">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={loading}
          >
            {loading
              ? "Logging in..."
              : "Login"}
          </button>
        </form>
      </div>
    </div>
  );
}