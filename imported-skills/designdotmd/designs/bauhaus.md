---
version: alpha
name: Bauhaus
description: Primary red, primary yellow, primary blue.
colors:
  primary: "#121212"
  secondary: "#585858"
  tertiary: "#E53935"
  neutral: "#F4EFE4"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Archivo
    fontSize: 4.5rem
    fontWeight: 800
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Archivo
    fontSize: 2.5rem
    fontWeight: 800
  body:
    fontFamily: Archivo
    fontSize: 1rem
    lineHeight: 1.55
  label:
    fontFamily: Archivo
    fontSize: 0.75rem
    letterSpacing: "0.08em"
rounded:
  sm: 0px
  md: 0px
  lg: 0px
spacing:
  sm: 8px
  md: 16px
  lg: 32px
components:
  button-primary:
    backgroundColor: "{colors.tertiary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.md}"
    padding: 12px 20px
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.lg}"
    padding: 24px
---
## Overview

A geometric, primary-color system. Flat planes, heavy sans, no shadows. Pay attention to the shape.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#121212`):** Headlines and core text.
- **Secondary (`#585858`):** Borders, captions, and metadata.
- **Tertiary (`#E53935`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F4EFE4`):** The page foundation.

## Typography

- **display:** Archivo 4.5rem
- **h1:** Archivo 2.5rem
- **body:** Archivo 1rem
- **label:** Archivo 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
