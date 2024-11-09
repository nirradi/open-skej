import React from "react";
import { styled } from "@mui/system";
import Box from "@mui/material/Box";
import { useSelector } from "react-redux";
import { calendarSlotsActions, DayOfWeek, RootState, SlotId } from "../store";
import Slot from "./slot";
import { useDispatch } from "react-redux";

const CalendarContainer = styled(Box)({
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  width: "100%",
  border: "1px solid #ccc",
  borderRadius: "8px",
  overflow: "hidden",
});

const CalendarHeader = styled(Box)({
  display: "flex",
  width: "100%",
  backgroundColor: "#f5f5f5",
  borderBottom: "1px solid #ccc",
});

const CalendarHeaderDay = styled(Box)({
  flex: 1,
  padding: "8px",
  textAlign: "center",
  fontWeight: "bold",
});

const CalendarBody = styled(Box)({
  display: "flex",
  flexDirection: "column",
  width: "100%",
});

const CalendarRow = styled(Box)({
  display: "flex",
  width: "100%",
});

const daysOfWeek: DayOfWeek[] = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

const Calendar: React.FC = () => {
  const calendarConfig = useSelector((state: RootState) => state.admin.calendarConfig);
  const calendarSlots = useSelector((state: RootState) => state.calendarSlots.slots);
  const currentUser = useSelector((state: RootState) => state.user);
  const isSlotAvailable = React.useCallback(
    (day: DayOfWeek, hour: string) => {
      const slotId: SlotId = `${day}-${hour}`;
      return !calendarSlots[slotId];
    },
    [calendarSlots]
  );
  const userInSlot = React.useCallback(
    (day: DayOfWeek, hour: string) => {
      const slotId: SlotId = `${day}-${hour}`;
      return calendarSlots[slotId];
    },
    [calendarSlots]
  );

  const dispatch = useDispatch();
  const handleSlotClick = React.useCallback(
    (day: DayOfWeek, hour: string) => {
      if (!currentUser.userId) return;
      const slotId: SlotId = `${day}-${hour}`;
      // if slot is already booked, clear slot
      if (!isSlotAvailable(day, hour)) {
        dispatch(
          calendarSlotsActions.clearSlotUser({
            slotId,
          })
        );
        return;
      }
      dispatch(
        calendarSlotsActions.setSlotUser({
          slotId,
          userId: currentUser.userId,
        })
      );
    },
    [dispatch, isSlotAvailable, currentUser.userId]
  );

  const { firstHour, lastHour, smallestSlotDuration } = calendarConfig;
  const hours = [];
  for (let hour = firstHour; hour <= lastHour; hour += smallestSlotDuration / 60) {
    hours.push(`${Math.floor(hour)}:${(hour % 1) * 60 === 0 ? "00" : (hour % 1) * 60}`);
  }

  const firstDayOfWeek = calendarConfig.firstDayOfWeek; // Assuming this is configured in calendarConfig
  const days = [...daysOfWeek.slice(firstDayOfWeek), ...daysOfWeek.slice(0, firstDayOfWeek)];

  return (
    <CalendarContainer>
      <CalendarHeader>
        {days.map(day => (
          <CalendarHeaderDay key={day}>{day}</CalendarHeaderDay>
        ))}
      </CalendarHeader>
      <CalendarBody>
        {hours.map(hour => (
          <CalendarRow key={hour}>
            {days.map(day => (
              <Slot
                onClick={() => handleSlotClick(day, hour)}
                key={day}
                hour={hour}
                isAvailable={isSlotAvailable(day, hour)}
                allowClickWhenUnavailable={currentUser.isAdmin || userInSlot(day, hour) === currentUser.userId}
              />
            ))}
          </CalendarRow>
        ))}
      </CalendarBody>
    </CalendarContainer>
  );
};

export default Calendar;
