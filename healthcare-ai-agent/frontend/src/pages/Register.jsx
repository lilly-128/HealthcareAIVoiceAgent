import { useState } from 'react'
import { api } from '../api'

export default function Register({ onRegistered, onLogin }) {
  const [form, setForm] = useState({
    name: '',
    phone: '',
    email: '',
    password: '',
    date_of_birth: '',
  })

  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  function handleChange(e) {
    setForm({
      ...form,
      [e.target.name]: e.target.value,
    })
  }

  async function handleSubmit(e) {
    e.preventDefault()
    setError('')

    if (!form.name || !form.phone || !form.password) {
      setError('Name, phone and password are required.')
      return
    }

    try {
      setLoading(true)

      const data = await api.registerPatient({
        name: form.name,
        phone: form.phone,
        email: form.email || null,
        password: form.password,
        date_of_birth: form.date_of_birth || null,
      })

      localStorage.setItem('role', data.role)

      onRegistered(data.role)

    } catch (err) {
      setError(err.message || 'Registration failed.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="login">
      <h1>Create Patient Account</h1>

      <div className="card">

        <input
          type="text"
          name="name"
          placeholder="Full Name"
          value={form.name}
          onChange={handleChange}
        />

        <input
          type="tel"
          name="phone"
          placeholder="Phone"
          value={form.phone}
          onChange={handleChange}
        />

        <input
          type="email"
          name="email"
          placeholder="Email (optional)"
          value={form.email}
          onChange={handleChange}
        />

        <input
          type="password"
          name="password"
          placeholder="Password"
          value={form.password}
          onChange={handleChange}
        />

        <input
          type="date"
          name="date_of_birth"
          value={form.date_of_birth}
          onChange={handleChange}
        />

        <button onClick={handleSubmit} disabled={loading}>
          {loading ? 'Creating Account...' : 'Create Account'}
        </button>

        {error && (
          <p className="error">
            {error}
          </p>
        )}

        <p className="hint">
          Already have an account?
        </p>

        <button onClick={onLogin}>
          Back to Login
        </button>

      </div>
    </main>
  )
}