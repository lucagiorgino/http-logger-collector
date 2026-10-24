export function PageHeading({ eyebrow, title, description, children }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-5">
      <div>
        <p className="eyebrow mb-3">{eyebrow}</p>
        <h1 className="page-title">{title}</h1>
        <p className="mt-3 max-w-2xl text-sm leading-6 text-muted-foreground">
          {description}
        </p>
      </div>
      {children}
    </div>
  );
}
