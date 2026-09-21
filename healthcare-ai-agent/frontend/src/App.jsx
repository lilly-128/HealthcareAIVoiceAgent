import { useState } from 'react'
import { api } from './api'

import Patient from './pages/Patient'
import Doctor from './pages/Doctor'
import Admin from './pages/Admin'

import Register from './pages/Register'
import HospitalRegister from './pages/HospitalRegister'


export default function App() {

  const [role, setRole] = useState(
    localStorage.getItem('role') || ''
  )

  const [showRegister, setShowRegister] = useState(false)

  const [registerType, setRegisterType] = useState('')

  const [email, setEmail] = useState(
    'lilly@patient.test'
  )

  const [password, setPassword] = useState(
    'demo1234'
  )

  const [error, setError] = useState('')


  // ==================================================
  // LOGIN
  // ==================================================

  const login = async () => {

    setError('')

    try {

      const r = await api.login(
        email,
        password
      )

      console.log('LOGIN RESPONSE:', r)


      if (!r.access_token) {

        throw new Error(
          'Login successful but no access token was received.'
        )

      }


      // Save token
      localStorage.setItem(
        'token',
        r.access_token
      )


      // Save role
      localStorage.setItem(
        'role',
        r.role || ''
      )


      // Save hospital ID
      if (r.hospital_id) {

        localStorage.setItem(
          'hospital_id',
          r.hospital_id
        )

      } else {

        localStorage.removeItem(
          'hospital_id'
        )

      }


      // Save patient ID
      if (r.patient_id) {

        localStorage.setItem(
          'patient_id',
          r.patient_id
        )

      } else {

        localStorage.removeItem(
          'patient_id'
        )

      }


      // Save doctor ID
      if (r.doctor_id) {

        localStorage.setItem(
          'doctor_id',
          r.doctor_id
        )

      } else {

        localStorage.removeItem(
          'doctor_id'
        )

      }


      // Debug
      console.log(
        'TOKEN SAVED:',
        localStorage.getItem('token')
          ? 'YES'
          : 'NO'
      )

      console.log(
        'ROLE:',
        localStorage.getItem('role')
      )

      console.log(
        'HOSPITAL ID:',
        localStorage.getItem('hospital_id')
      )

      console.log(
        'PATIENT ID:',
        localStorage.getItem('patient_id')
      )

      console.log(
        'DOCTOR ID:',
        localStorage.getItem('doctor_id')
      )


      setRole(r.role)

    } catch (err) {

      console.error(
        'LOGIN ERROR:',
        err
      )

      setError(
        err.message ||
        'That email and password did not match an account.'
      )

    }

  }


  // ==================================================
  // LOGOUT
  // ==================================================

  const logout = () => {

    localStorage.removeItem('token')
    localStorage.removeItem('role')
    localStorage.removeItem('hospital_id')
    localStorage.removeItem('patient_id')
    localStorage.removeItem('doctor_id')

    setRole('')

    setShowRegister(false)

    setRegisterType('')

    setError('')

  }


  // ==================================================
  // REGISTRATION PAGES
  // ==================================================

  if (showRegister && !role) {


    // -----------------------------------------------
    // PATIENT REGISTRATION
    // -----------------------------------------------

    if (registerType === 'patient') {

      return (

        <Register

          onRegistered={(newRole) => {

            setRole(newRole)

            setShowRegister(false)

            setRegisterType('')

          }}

          onLogin={() => {

            setShowRegister(false)

            setRegisterType('')

          }}

        />

      )

    }


    // -----------------------------------------------
    // HOSPITAL REGISTRATION
    // -----------------------------------------------

    if (registerType === 'hospital') {

      return (

        <HospitalRegister

          onRegistered={() => {

            alert(
              'Hospital registration submitted successfully. Please wait for Platform Admin approval.'
            )

            setShowRegister(false)

            setRegisterType('')

          }}

          onLogin={() => {

            setShowRegister(false)

            setRegisterType('')

          }}

        />

      )

    }


    

    // -----------------------------------------------
    // ACCOUNT TYPE SELECTION
    // -----------------------------------------------

    return (

      <main className="login">

        <h1>
          Create Account
        </h1>


        <div className="card">

          <h2>
            Select account type
          </h2>


          <button
            type="button"
            onClick={() => {
              setRegisterType('patient')
            }}
          >
            Patient Registration
          </button>


          <button
            type="button"
            onClick={() => {
              setRegisterType('hospital')
            }}
          >
            Hospital Registration
          </button>


          <button
            type="button"
            onClick={() => {

              setShowRegister(false)

              setRegisterType('')

            }}
          >
            Back to Login
          </button>

        </div>

      </main>

    )

  }


  // ==================================================
  // LOGIN PAGE
  // ==================================================

  if (!role) {

    return (

      <main className="login">

        <h1>
          Patient access platform
        </h1>


        <div className="card">

          <input
            value={email}
            onChange={(e) =>
              setEmail(e.target.value)
            }
            placeholder="Email"
          />


          <input
            type="password"
            value={password}
            onChange={(e) =>
              setPassword(e.target.value)
            }
            placeholder="Password"
          />


          <button
            onClick={login}
          >
            Sign in
          </button>


          {error && (

            <p className="error">
              {error}
            </p>

          )}


          <button
            type="button"
            onClick={() => {

              setError('')

              setRegisterType('')

              setShowRegister(true)

            }}
          >
            Create Account
          </button>


          <p className="hint">

            Demo accounts, password{' '}

            <code>
              demo1234
            </code>

            <br />

            lilly@patient.test ·{' '}

            doctor11@hospital.test ·{' '}

            admin1@hospital.test ·{' '}

            admin@platform.test

          </p>

        </div>

      </main>

    )

  }


  // ==================================================
  // LOGGED-IN DASHBOARD
  // ==================================================

  return (

    <main>

      <header>

        <h1>
          Patient access platform
        </h1>


        <div>

          <span className="tag">

            {role
              .replace('_', ' ')
              .toLowerCase()
            }

          </span>


          <button
            onClick={logout}
          >
            Sign out
          </button>

        </div>

      </header>


      {/* PATIENT */}

      {role === 'PATIENT' && (

        <Patient />

      )}


      {/* DOCTOR */}

      {role === 'DOCTOR' && (

        <Doctor />

      )}


      {/* PLATFORM ADMIN / HOSPITAL ADMIN */}

      {(
        role === 'PLATFORM_ADMIN' ||
        role === 'HOSPITAL_ADMIN'
      ) && (

        <Admin />

      )}

    </main>

  )

}