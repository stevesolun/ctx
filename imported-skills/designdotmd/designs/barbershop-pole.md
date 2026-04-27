---
version: alpha
name: Barbershop Pole
description: Classic barbershop: stripe red, ivory lather, nickel.
colors:
  primary: "#15100D"
  secondary: "#7A6E5E"
  tertiary: "#B83A3A"
  neutral: "#F3ECD9"
  surface: "#FBF5E2"
  on-primary: "#FBF5E2"
typography:
  display:
    fontFamily: Playfair Display
    fontSize: 4.5rem
    fontWeight: 700
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Playfair Display
    fontSize: 2.3rem
    fontWeight: 600
  body:
    fontFamily: Lora
    fontSize: 1rem
    lineHeight: 1.7
  label:
    fontFamily: Lora
    fontSize: 0.75rem
    fontWeight: 600
    letterSpacing: "0.18em"
rounded:
  sm: 2px
  md: 4px
  lg: 6px
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

A classic barbershop palette: lather ivory, barber-pole red stripe, deep brass accent.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#15100D`):** Headlines and core text.
- **Secondary (`#7A6E5E`):** Borders, captions, and metadata.
- **Tertiary (`#B83A3A`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F3ECD9`):** The page foundation.

## Typography

- **display:** Playfair Display 4.5rem
- **h1:** Playfair Display 2.3rem
- **body:** Lora 1rem
- **label:** Lora 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
