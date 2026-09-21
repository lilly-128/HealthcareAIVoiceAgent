import { useEffect, useState } from 'react'
import { api, setFault } from '../api'
import DoctorRegister from './DoctorRegister'

export default function Admin() {
  const role = localStorage.getItem('role')
  const hospitalId = localStorage.getItem('hospital_id')

  const isPlatformAdmin = role === 'PLATFORM_ADMIN'
  const isHospitalAdmin = role === 'HOSPITAL_ADMIN'

  const [overview, setOverview] = useState(null)
  const [pending, setPending] = useState([])
  const [appointments, setAppointments] = useState([])
  const [ops, setOps] = useState([])
  const [recon, setRecon] = useState([])
  const [audit, setAudit] = useState([])
  const [timeline, setTimeline] = useState(null)

  const [note, setNote] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  // Doctor registration screen
  const [showDoctorRegister, setShowDoctorRegister] =
    useState(false)

  async function refresh() {
    setLoading(true)
    setError('')

    try {
      const [
        overviewData,
        appointmentData,
        opsData,
        reconData,
        auditData,
      ] = await Promise.all([
        api.overview(),
        api.adminAppointments(),
        api.integrationOps(),
        api.reconciliations(),
        api.auditLog(),
      ])

      console.log('ADMIN OVERVIEW:', overviewData)

      setOverview(overviewData)
      setAppointments(appointmentData || [])
      setOps(opsData || [])
      setRecon(reconData || [])
      setAudit(auditData || [])

      if (isPlatformAdmin) {
        const pendingData = await api.pendingHospitals()
        setPending(pendingData || [])
      } else {
        setPending([])
      }
    } catch (err) {
      console.error(err)

      setError(
        err.message || 'Failed to load admin dashboard'
      )
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
  }, [])

  async function injectFault() {
    try {
      await api.runWorkflows()

      setFault(true)
      setNote('EHR workflow executed.')

      await refresh()
    } catch (err) {
      setError(
        err.message || 'Failed to run workflow'
      )
    }
  }

  async function approveHospital(id) {
    try {
      await api.approve(id)

      setNote('Hospital approved.')

      await refresh()
    } catch (err) {
      setError(
        err.message || 'Failed to approve hospital'
      )
    }
  }

  async function rejectHospital(id) {
    try {
      await api.reject(id)

      setNote('Hospital rejected.')

      await refresh()
    } catch (err) {
      setError(
        err.message || 'Failed to reject hospital'
      )
    }
  }

  async function resolveRecon(id) {
    try {
      await api.resolveRecon(id)

      setNote('Reconciliation resolved.')

      await refresh()
    } catch (err) {
      setError(
        err.message ||
          'Failed to resolve reconciliation'
      )
    }
  }

  async function showTrace(id) {
    try {
      const data = await api.trace(id)

      setTimeline(data)
    } catch (err) {
      setError(
        err.message || 'Failed to load trace'
      )
    }
  }

  /*
   * =========================================
   * PLATFORM ADMIN -> DOCTOR REGISTRATION
   * =========================================
   *
   * Doctor registration is available from the
   * complete Platform Admin dashboard.
   *
   * Platform Admin must already be logged in,
   * so api.js will send the JWT token.
   */

  if (isPlatformAdmin && showDoctorRegister) {
    return (
      <div className="grid">

        <section className="card">

          <DoctorRegister
            onDone={() => {
              setShowDoctorRegister(false)
            }}

            onRegistered={() => {
              setNote(
                'Doctor registered successfully.'
              )

              setShowDoctorRegister(false)

              refresh()
            }}

            onLogin={() => {
              setShowDoctorRegister(false)
            }}
          />

          <button
            type="button"
            onClick={() => {
              setShowDoctorRegister(false)
            }}
          >
            ← Back to Platform Admin Dashboard
          </button>

        </section>

      </div>
    )
  }

  /*
   * Helper for safely displaying overview values.
   */

  function count(key) {
    if (!overview) {
      return 0
    }

    return overview[key] ?? 0
  }

  return (
    <div className="grid">

      {/* =====================================
          HEADER
      ===================================== */}

      <section className="card">

        <h2>
          {isHospitalAdmin
            ? 'Hospital Admin Dashboard'
            : 'Platform Admin Dashboard'}
        </h2>

        {isHospitalAdmin && (
          <p>
            <strong>Hospital ID:</strong>{' '}
            {hospitalId}
          </p>
        )}

        {isPlatformAdmin && (
          <p>
            <strong>Role:</strong>{' '}
            Platform Administrator
          </p>
        )}

        {note && (
          <p>
            <strong>{note}</strong>
          </p>
        )}

        {error && (
          <p className="error">
            {error}
          </p>
        )}

        <div className="row">

          <button
            type="button"
            onClick={refresh}
            disabled={loading}
          >
            {loading
              ? 'Refreshing...'
              : 'Refresh Dashboard'}
          </button>


          {/* =================================
              REGISTER DOCTOR
              PLATFORM ADMIN ONLY
          ================================= */}

          {isPlatformAdmin && (
            <button
              type="button"
              onClick={() => {
                setShowDoctorRegister(true)
              }}
            >
              + Register Doctor
            </button>
          )}

        </div>

      </section>


      {/* =====================================
          PLATFORM / HOSPITAL STATISTICS
      ===================================== */}

      <section className="card">

        <h3>
          {isHospitalAdmin
            ? 'Hospital Statistics'
            : 'Platform Statistics'}
        </h3>

        <div className="grid">

          {/* HOSPITALS */}

          <div className="card">
            <h3>Total Hospitals</h3>

            <h1>
              {count('hospitals')}
            </h1>
          </div>


          {/* DOCTORS */}

          <div className="card">
            <h3>Total Doctors</h3>

            <h1>
              {count('doctors')}
            </h1>
          </div>


          {/* PATIENTS */}

          <div className="card">
            <h3>Total Patients</h3>

            <h1>
              {count('patients')}
            </h1>
          </div>


          {/* APPOINTMENTS */}

          <div className="card">
            <h3>Total Appointments</h3>

            <h1>
              {count('appointments_total')}
            </h1>
          </div>


          {/* AI CONVERSATIONS */}

          <div className="card">
            <h3>AI Conversations</h3>

            <h1>
              {count('ai_conversations')}
            </h1>
          </div>


          {/* CAPABILITY CALLS */}

          <div className="card">
            <h3>Capability Calls</h3>

            <h1>
              {count('capability_calls')}
            </h1>
          </div>


          {/* CAPABILITY FAILURES */}

          <div className="card">
            <h3>Capability Failures</h3>

            <h1>
              {count('capability_failures')}
            </h1>
          </div>


          {/* INTEGRATION OPERATIONS */}

          <div className="card">
            <h3>Integration Operations</h3>

            <h1>
              {count('integration_ops')}
            </h1>
          </div>


          {/* INTEGRATION FAILURES */}

          <div className="card">
            <h3>Integration Failures</h3>

            <h1>
              {count('integration_failures')}
            </h1>
          </div>


          {/* RECONCILIATIONS */}

          <div className="card">
            <h3>Open Reconciliations</h3>

            <h1>
              {count('reconciliations_open')}
            </h1>
          </div>


          {/* NOTIFICATIONS */}

          <div className="card">
            <h3>Notifications</h3>

            <h1>
              {count('notifications')}
            </h1>
          </div>

        </div>

      </section>


      {/* =====================================
          APPOINTMENT STATUS
      ===================================== */}

      <section className="card">

        <h3>Appointments by Status</h3>

        {overview?.appointments_by_status ? (
          <pre>
            {JSON.stringify(
              overview.appointments_by_status,
              null,
              2
            )}
          </pre>
        ) : (
          <p>
            No appointment status data.
          </p>
        )}

      </section>


      {/* =====================================
          WORKFLOW STATUS
      ===================================== */}

      <section className="card">

        <h3>Workflows by State</h3>

        {overview?.workflows_by_state ? (
          <pre>
            {JSON.stringify(
              overview.workflows_by_state,
              null,
              2
            )}
          </pre>
        ) : (
          <p>
            No workflow data.
          </p>
        )}

      </section>


      {/* =====================================
          HOSPITAL APPLICATIONS
      ===================================== */}

      {isPlatformAdmin && (
        <section className="card">

          <h3>Hospital Applications</h3>

          {pending.length === 0 ? (
            <p>
              No pending hospital applications.
            </p>
          ) : (
            pending.map((hospital) => (
              <div
                key={hospital.id}
                className="card"
              >

                <p>
                  <strong>
                    {hospital.name}
                  </strong>
                </p>

                <p>
                  Hospital ID: {hospital.id}
                </p>

                <p>
                  Status: {hospital.status}
                </p>

                <div className="row">

                  <button
                    type="button"
                    onClick={() =>
                      approveHospital(
                        hospital.id
                      )
                    }
                  >
                    Approve
                  </button>

                  <button
                    type="button"
                    onClick={() =>
                      rejectHospital(
                        hospital.id
                      )
                    }
                  >
                    Reject
                  </button>

                </div>

              </div>
            ))
          )}

        </section>
      )}


      {/* =====================================
          EHR CONTROLS
      ===================================== */}

      <section className="card">

        <h3>EHR Controls</h3>

        <button
          type="button"
          onClick={injectFault}
        >
          Run EHR Workflow
        </button>

      </section>


      {/* =====================================
          APPOINTMENTS
      ===================================== */}

      <section className="card">

        <h3>Appointments</h3>

        {appointments.length === 0 ? (
          <p>
            No appointments found.
          </p>
        ) : (
          <pre>
            {JSON.stringify(
              appointments,
              null,
              2
            )}
          </pre>
        )}

      </section>


      {/* =====================================
          APPOINTMENT TRACE
      ===================================== */}

      <section className="card">

        <h3>Appointment Trace</h3>

        {appointments.length === 0 ? (
          <p>
            No appointments available.
          </p>
        ) : (
          <>
            <p>
              Select an appointment to
              view its trace.
            </p>

            {appointments.map(
              (appointment) => (
                <div
                  key={appointment.id}
                  className="row"
                >

                  <span>
                    Appointment:{' '}
                    {appointment.id}
                  </span>

                  <button
                    type="button"
                    onClick={() =>
                      showTrace(
                        appointment.id
                      )
                    }
                  >
                    View Trace
                  </button>

                </div>
              )
            )}
          </>
        )}

        {timeline && (
          <pre>
            {JSON.stringify(
              timeline,
              null,
              2
            )}
          </pre>
        )}

      </section>


      {/* =====================================
          INTEGRATION OPERATIONS
      ===================================== */}

      <section className="card">

        <h3>Integration Operations</h3>

        {ops.length === 0 ? (
          <p>
            No integration operations.
          </p>
        ) : (
          <pre>
            {JSON.stringify(
              ops,
              null,
              2
            )}
          </pre>
        )}

      </section>


      {/* =====================================
          RECONCILIATION QUEUE
      ===================================== */}

      <section className="card">

        <h3>Reconciliation Queue</h3>

        {recon.length === 0 ? (
          <p>
            No reconciliation items.
          </p>
        ) : (
          recon.map((item) => (
            <div
              key={item.id}
              className="card"
            >

              <pre>
                {JSON.stringify(
                  item,
                  null,
                  2
                )}
              </pre>

              <button
                type="button"
                onClick={() =>
                  resolveRecon(item.id)
                }
              >
                Resolve
              </button>

            </div>
          ))
        )}

      </section>


      {/* =====================================
          AUDIT LOG
      ===================================== */}

      <section className="card">

        <h3>Audit Log</h3>

        {audit.length === 0 ? (
          <p>
            No audit records.
          </p>
        ) : (
          <pre>
            {JSON.stringify(
              audit,
              null,
              2
            )}
          </pre>
        )}

      </section>

    </div>
  )
}