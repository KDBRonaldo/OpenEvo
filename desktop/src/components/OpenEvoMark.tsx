import openEvoFavicon from "../../../assets/openevo-favicon.svg";

export function OpenEvoMark({ className }: { className?: string }) {
  return (
    <img
      className={className}
      src={openEvoFavicon}
      alt=""
      aria-hidden="true"
      draggable={false}
    />
  );
}
