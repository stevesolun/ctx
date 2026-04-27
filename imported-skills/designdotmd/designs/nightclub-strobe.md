---
version: alpha
name: Nightclub Strobe
description: Berlin techno: stark black, strobe white, bass magenta.
colors:
  primary: "#F6F6F6"
  secondary: "#7A7A7A"
  tertiary: "#FF2A7F"
  neutral: "#000000"
  surface: "#0A0A0A"
  on-primary: "#000000"
typography:
  display:
    fontFamily: Archivo Black
    fontSize: 5rem
    fontWeight: 900
    letterSpacing: "-0.04em"
  h1:
    fontFamily: Archivo Black
    fontSize: 2.4rem
    fontWeight: 900
  body:
    fontFamily: Inter
    fontSize: 0.92rem
    lineHeight: 1.5
  label:
    fontFamily: Archivo
    fontSize: 0.7rem
    fontWeight: 700
    letterSpacing: "0.22em"
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

A techno-club palette: stark monochrome with one strobe-magenta that punches through.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#F6F6F6`):** Headlines and core text.
- **Secondary (`#7A7A7A`):** Borders, captions, and metadata.
- **Tertiary (`#FF2A7F`):** The sole driver for interaction. Reserve it.
- **Neutral (`#000000`):** The page foundation.

## Typography

- **display:** Archivo Black 5rem
- **h1:** Archivo Black 2.4rem
- **body:** Inter 0.92rem
- **label:** Archivo 0.7rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
