import { NavBar, NoteUICollection, UpdateNote, CreateNote } from './ui-components'
import './App.css';
import { useState } from 'react'
import { DataStore } from 'aws-amplify';
import { withAuthenticator } from '@aws-amplify/ui-react'

function App( {signOut} ) {
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [showUpdateModal, setShowUpdateModal] = useState(false)
  const [updateNote, setUpdateNote] = useState()
  return (
    <>
      <NavBar marginBottom='20px' overrides={{ 
        Button31632483: { onClick: () => setShowCreateModal(true) },
        Button31632487: { onClick: async() => {
          await DataStore.clear()   // clear out the local data when a user sign out, then DataStore will query the db instead of going to the local data first
          signOut()
        }}
        }}/>
      <div className='container'>
        <NoteUICollection overrideItems={({ item, idx }) => {
          return {
            overrides: {
              EditButton: {
                as: 'button',
                onClick: () => {
                  setShowUpdateModal(true)
                  setUpdateNote(item)
                }
              }
            }
          }
        }}/>
      </div>
      <div className='modal' style={{display: showCreateModal === false && 'none'}}>
        <CreateNote overrides={{
          MyIcon: {
            as: 'button',
            onClick: () => setShowCreateModal(false)
          }
        }}/>
      </div>
      <div className='modal' style={{display: showUpdateModal === false && 'none'}}>
        <UpdateNote overrides={{
          MyIcon: {
            as: 'button',
            onClick: () => setShowUpdateModal(false)
          }
        }}/>
      </div>
    </>
  );
}

export default withAuthenticator(App);
