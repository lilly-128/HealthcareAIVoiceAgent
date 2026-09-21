import { useState } from 'react'
import { api } from '../api'

export default function DoctorRegister({ onDone }) {

  // Get hospital ID of the logged-in Hospital Admin
  const hospitalId = localStorage.getItem('hospital_id') || ''

  const [form, setForm] = useState({
    hospital_id: '',
    name: '',
    email: '',
    phone: '',
    specialty: '',
    department: '',
    qualifications: '',
    experience: '',
    languages: '',
    password: '',
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
      !form.hospital_id ||
      !form.name ||
      !form.email ||
      !form.specialty ||
      !form.password
    ) {
      setError(
        'Hospital ID, doctor name, email, specialty and password are required.'
      )
      return
    }

    try {
      setLoading(true)

      await api.registerDoctor({
        hospital_id: form.hospital_id,
        name: form.name,
        email: form.email,
        phone: form.phone,
        specialty: form.specialty,
        department: form.department,
        qualifications: form.qualifications,
        experience: Number(form.experience) || 0,
        languages: form.languages,
        password: form.password,
      })

      setMessage(
        'Doctor registered successfully.'
      )

      setForm({
        hospital_id: '',
        name: '',
        email: '',
        phone: '',
        specialty: '',
        department: '',
        qualifications: '',
        experience: '',
        languages: '',
        password: '',
      })

    } catch (err) {
      setError(
        err.message || 'Doctor registration failed.'
      )
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="card">

      <h2>Register Doctor</h2>

      {/* Hospital ID */}
      <input 
        type="text" 
        name="hospital_id" 
        placeholder="Hospital ID" 
        value={form.hospital_id} 
        onChange={handleChange}
      />

      <input
        type="text"
        name="name"
        placeholder="Doctor Name"
        value={form.name}
        onChange={handleChange}
      />

      <input
        type="email"
        name="email"
        placeholder="Doctor Email"
        value={form.email}
        onChange={handleChange}
      />

      <input
        type="tel"
        name="phone"
        placeholder="Doctor Phone"
        value={form.phone}
        onChange={handleChange}
      />

      <input
        type="text"
        name="specialty"
        placeholder="Specialty"
        value={form.specialty}
        onChange={handleChange}
      />

      <input
        type="text"
        name="department"
        placeholder="Department"
        value={form.department}
        onChange={handleChange}
      />

      <input
        type="text"
        name="qualifications"
        placeholder="Qualifications"
        value={form.qualifications}
        onChange={handleChange}
      />

      <input
        type="text"
        name="experience"
        placeholder="Experience"
        value={form.experience}
        onChange={handleChange}
      />

      <input
        type="text"
        name="languages"
        placeholder="Languages (comma separated)"
        value={form.languages}
        onChange={handleChange}
      />

      <input
        type="password"
        name="password"
        placeholder="Doctor Password"
        value={form.password}
        onChange={handleChange}
      />

      <button
        onClick={handleSubmit}
        disabled={loading}
      >
        {loading ? 'Creating...' : 'Register Doctor'}
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

      {onDone && (
        <button
          type="button"
          onClick={onDone}
        >
          Back
        </button>
      )}

    </div>
  )
}