import React from "react";
import { styled } from "@mui/system";

interface SlotProps {
  hour: string;
  isAvailable: boolean;
  allowClickWhenUnavailable: boolean;
  onClick: () => void;
}

const SlotContainer = styled("div")<{ isAvailable: boolean; allowClickWhenUnavailable: boolean }>(
  ({ isAvailable, allowClickWhenUnavailable }) => ({
    flex: 1,
    padding: "10px",
    border: "1px solid #ccc",
    borderRadius: "4px",
    width: "100px",
    height: "40px",
    backgroundColor: isAvailable ? "#e0ffe0" : "#ffe0e0",
    cursor: isAvailable || allowClickWhenUnavailable ? "pointer" : "default",
  })
);

const Hour = styled("div")({
  fontSize: "14px",
  fontWeight: "bold",
});

const Slot: React.FC<SlotProps> = ({ hour, isAvailable = false, allowClickWhenUnavailable = false, onClick }) => {
  const handleClick = () => {
    if (isAvailable || allowClickWhenUnavailable) {
      onClick();
    }
  };

  return (
    <SlotContainer
      isAvailable={isAvailable}
      allowClickWhenUnavailable={allowClickWhenUnavailable}
      onClick={handleClick}
    >
      <Hour>{hour}</Hour>
    </SlotContainer>
  );
};

export default Slot;
