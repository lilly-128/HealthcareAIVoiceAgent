import { useState } from 'react'
import { api } from '../api'

export default function HospitalRegister({ onLogin }) {
  const [form, setForm] = useState({
    hospital_name: '',
    address: '',
    city: '',
    phone: '',
    departments: '',
    specialties: '',

    admin_name: '',
    admin_email: '',
    admin_phone: '',
    admin_password: '',
  })

  const [loading, setLoading] = useState(false)
  const [message, setMessage] = useState('')
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
    setMessage('')

    if (
      !form.hospital_name ||
      !form.city ||
      !form.admin_name ||
      !form.admin_email ||
      !form.admin_password
    ) {
      setError(
        'Hospital name, city, admin name, admin email and admin password are required.'
      )
      return
    }

    try {
      setLoading(true)

      await api.registerHospital({
        hospital_name: form.hospital_name,
        address: form.address,
        city: form.city,
        phone: form.phone,

        departments: form.departments
          .split(',')
          .map(item => item.trim())
          .filter(Boolean),

        specialties: form.specialties
          .split(',')
          .map(item => item.trim())
          .filter(Boolean),

        admin_name: form.admin_name,
        admin_email: form.admin_email,
        admin_phone: form.admin_phone,
        admin_password: form.admin_password,
      })

      setMessage(
        'Hospital registration submitted successfully. Please wait for Platform Admin approval.'
      )

      setForm({
        hospital_name: '',
        address: '',
        city: '',
        phone: '',
        departments: '',
        specialties: '',

        admin_name: '',
        admin_email: '',
        admin_phone: '',
        admin_password: '',
      })
    } catch (err) {
      setError(err.message || 'Hospital registration failed.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="login">

      <h1>Register Hospital</h1>

      <div className="card">

        <h2>Hospital Details</h2>

        <input
          type="text"
          name="hospital_name"
          placeholder="Hospital Name"
          value={form.hospital_name}
          onChange={handleChange}
        />

        <input
          type="text"
          name="address"
          placeholder="Hospital Address"
          value={form.address}
          onChange={handleChange}
        />

        <input
          type="text"
          name="city"
          placeholder="City"
          value={form.city}
          onChange={handleChange}
        />

        <input
          type="tel"
          name="phone"
          placeholder="Hospital Phone"
          value={form.phone}
          onChange={handleChange}
        />

        <input
          type="text"
          name="departments"
          placeholder="Departments (comma separated)"
          value={form.departments}
          onChange={handleChange}
        />

        <input
          type="text"
          name="specialties"
          placeholder="Specialties (comma separated)"
          value={form.specialties}
          onChange={handleChange}
        />

        <h2>Hospital Admin Details</h2>

        <input
          type="text"
          name="admin_name"
          placeholder="Admin Name"
          value={form.admin_name}
          onChange={handleChange}
        />

        <input
          type="email"
          name="admin_email"
          placeholder="Admin Email"
          value={form.admin_email}
          onChange={handleChange}
        />

        <input
          type="tel"
          name="admin_phone"
          placeholder="Admin Phone"
          value={form.admin_phone}
          onChange={handleChange}
        />

        <input
          type="password"
          name="admin_password"
          placeholder="Admin Password"
          value={form.admin_password}
          onChange={handleChange}
        />

        <button
          onClick={handleSubmit}
          disabled={loading}
        >
          {loading ? 'Submitting...' : 'Register Hospital'}
        </button>

        {message && (
          <p className="success">
            {message}
          </p>
        )}

        {error && (
          <p className="error">
            {error}
          </p>
        )}

        <p className="hint">
          Already have an account?
        </p>

        <button
          type="button"
          onClick={onLogin}
        >
          Back to Login
        </button>

      </div>
    </main>
  )
}