import "./App.css";
import "@fontsource/roboto/300.css";
import "@fontsource/roboto/400.css";
import "@fontsource/roboto/500.css";
import "@fontsource/roboto/700.css";
import { Typography } from "@mui/material";

import { Box } from "@mui/material";
import Calendar from "./Calendar";

import React, { useState } from "react";
import { Button, Modal, Box as MuiBox } from "@mui/material";

import { Provider, useDispatch } from "react-redux";
import store, { userActions } from "./store";
import { styled } from "@mui/system";

const StyledModalBox = styled(MuiBox)`
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: 400px;
  box-shadow: 24px;
  padding: 16px;
`;

const StyledButton = styled(Button)`
  margin: 8px;
  font-size: 0.875rem;
  padding: 4px 8px;
`;

function Sidebar() {
  const [open, setOpen] = useState(false);

  const handleOpen = () => setOpen(true);
  const handleClose = () => setOpen(false);
  const dispatch = useDispatch();

  return (
    <Box
      sx={{
        width: 250,
        height: "100vh",
        backgroundColor: "grey.200",
        padding: 2,
      }}
    >
      <Typography variant='h6'>Sidebar</Typography>
      <Button variant='contained' onClick={handleOpen}>
        Administration
      </Button>
      <Modal open={open} onClose={handleClose}>
        <StyledModalBox>
          <Typography variant='h6' component='h2'>
            Administration
          </Typography>
          <StyledButton
            variant='contained'
            onClick={() =>
              dispatch(
                userActions.setUser({
                  userId: "admin",
                  isAdmin: true,
                })
              )
            }
          >
            Switch to Admin User
          </StyledButton>
          <StyledButton
            variant='contained'
            onClick={() =>
              dispatch(
                userActions.setUser({
                  userId: "fake",
                  isAdmin: false,
                })
              )
            }
          >
            Switch to Fake User
          </StyledButton>
        </StyledModalBox>
      </Modal>
    </Box>
  );
}

function MainView() {
  return (
    <Box
      sx={{
        flexGrow: 1,
        padding: 2,
      }}
    >
      <Calendar />
    </Box>
  );
}

function App() {
  return (
    <Provider store={store}>
      <Box display='flex'>
        <Sidebar />
        <MainView />
      </Box>
    </Provider>
  );
}
export default App;
